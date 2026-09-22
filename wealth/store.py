"""Local, client-scoped evidence and decision storage.

The store deliberately has no network or model integration.  Text in sources and
facts is persisted as data and is never interpreted by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .situation.schema import SchemaError, out_of_range, validate as validate_canonical


SCHEMA_VERSION = 3
_CONFIDENCES = frozenset({"confirmed", "reported", "inferred"})
_SOURCE_KINDS = frozenset({"user", "document", "web", "tool", "inference", "connector", "pattern"})
# Sources that may not silently overwrite what the person told us (see ``remember``).
_CHALLENGER_KINDS = frozenset({"document", "web", "connector", "inference", "pattern"})
# Records that settle a figure outright (a statement, a payslip, a connected account), and the figures they settle.
_EVIDENCE_KINDS = frozenset({"document", "connector"})
_FIGURE_PREFIXES = ("income.", "cash.", "investment.", "liability.", "spending.", "account.")
_DECISION_STATUSES = frozenset({"accepted", "dismissed"})
# A fact revision is ``active`` (possibly closed by valid_to), ``corrected`` (it turned out
# wrong), or a value-less terminal row: ``forgotten`` (the person removed it) or
# ``replaced`` (a statement under another key took its place).
_TERMINAL_STATUSES = ("forgotten", "replaced")
_CONTRADICTION_CHOICES = ("keep", "use_new", "changed")
# A stated balance within this share of the statement is not worth a question.
STATED_TOLERANCE = 0.02
STATED_TOLERANCE_APPROXIMATE = 0.10
_TABLES = frozenset(
    {"metadata", "clients", "facts", "batches", "decisions", "decision_events", "auxiliary",
     "contradictions"}
)
_LEDGER_TABLES = frozenset({
    "ledger_accounts", "ledger_instruments", "ledger_entries", "ledger_fx", "ledger_assertions",
    "ledger_batches", "ledger_rules", "ledger_labels",
})
# Version 2 adds the transaction ledger.  Tables are additive; facts are untouched.
_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_accounts (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (client_id, id)
);
CREATE TABLE IF NOT EXISTS ledger_instruments (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (client_id, id)
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    date TEXT NOT NULL,
    amount TEXT,
    currency TEXT,
    instrument_id TEXT,
    quantity TEXT,
    dedupe_hash TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    reverses_id TEXT,
    data_json TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (client_id, id),
    UNIQUE (client_id, dedupe_hash),
    UNIQUE (client_id, seq)
);
CREATE INDEX IF NOT EXISTS ledger_entries_account_date
    ON ledger_entries(client_id, account_id, date);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_entries_one_reversal
    ON ledger_entries(client_id, reverses_id) WHERE reverses_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS ledger_fx (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    rate TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (client_id, date, base, quote)
);
CREATE TABLE IF NOT EXISTS ledger_assertions (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (client_id, id)
);
CREATE TABLE IF NOT EXISTS ledger_batches (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client_id, batch_id)
);
CREATE TABLE IF NOT EXISTS ledger_rules (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client_id, id)
);
CREATE TABLE IF NOT EXISTS ledger_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed','reported','inferred')),
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_labels_entry ON ledger_labels(client_id, entry_id, id);
"""
# Version 3 adds valid time to facts and pending contradictions.
_CONTRADICTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS contradictions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('same_key','stated_vs_statement')),
    key TEXT NOT NULL,
    proposed_key TEXT NOT NULL,
    current_fact_id TEXT NOT NULL,
    current_json TEXT NOT NULL,
    proposed_json TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','kept','used_new','changed','replaced')),
    request_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_json TEXT
);
CREATE INDEX IF NOT EXISTS contradictions_client_status
    ON contradictions(client_id, status, created_at);
CREATE INDEX IF NOT EXISTS contradictions_client_key
    ON contradictions(client_id, key, proposed_key);
CREATE INDEX IF NOT EXISTS facts_client_valid
    ON facts(client_id, key, valid_from);
"""
# The order audit trail (wealth/execution): append-only by trigger.  It is additive and
# created on open for any version-3 database, so the schema version does not change.
# Rows disappear only with their client (``delete_client`` cascades).
_ORDERS_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS orders (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    ticket_id TEXT NOT NULL,
    event TEXT NOT NULL,
    broker TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
    line INTEGER,
    client_order_id TEXT,
    broker_order_id TEXT,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS orders_client_ticket ON orders(client_id, ticket_id, seq)",
    """CREATE TRIGGER IF NOT EXISTS orders_append_only_update BEFORE UPDATE ON orders
BEGIN SELECT RAISE(ABORT, 'the orders audit trail is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS orders_append_only_delete BEFORE DELETE ON orders
WHEN EXISTS (SELECT 1 FROM clients WHERE id = OLD.client_id)
BEGIN SELECT RAISE(ABORT, 'the orders audit trail is append-only'); END""",
)
# The market-data cache (wealth/prices.py): shared by every client in this database,
# keyed by provider symbol, series kind and date.  Additive and created on open like
# ``orders``; the schema version does not change.  A past day's row fetched after that
# day is final (history is immutable); ``market_fetches`` records which ranges were read.
_MARKET_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS market_prices (
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('close','adjclose')),
    date TEXT NOT NULL,
    value TEXT NOT NULL,
    currency TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (symbol, kind, date)
)""",
    """CREATE TABLE IF NOT EXISTS market_fetches (
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    detail TEXT
)""",
    "CREATE INDEX IF NOT EXISTS market_fetches_symbol ON market_fetches(symbol, kind, retrieved_at)",
)
# The chat conversation (wealth/web.py): what the person and Wealth said, so a restart keeps
# the conversation and its Codex thread.  Additive and created on open like ``orders``; the
# schema version does not change.  ``conversations`` holds one row per conversation (the
# newest is current; "Nueva conversación" starts another and keeps the old one).
# ``conversation_messages`` is append-only: once written, a message's words, attachments and
# views never change (a trigger refuses it); only its memory receipts, which arrive after the
# reply is on screen, are filled in later.  Content and attachment names are redacted with
# ``ingest.redact.redact_text`` before they are written (RFC, CURP and SSN removed; CLABE,
# card and account numbers masked to their last four digits).  Rows disappear only with
# their client (``delete_client`` cascades) and travel in ``export_client``.  ``status`` marks
# a turn that did not finish: ``stopped`` (the person stopped it; the words streamed so far are
# the assistant message) or ``failed`` (the person's message went unanswered).  NULL is a
# finished exchange; the column is added to older databases on open.
_CONVERSATION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS conversations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (client_id, id)
)""",
    """CREATE TABLE IF NOT EXISTS conversation_messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    attachments_json TEXT,
    memory_json TEXT,
    views_json TEXT,
    created_at TEXT NOT NULL,
    status TEXT
)""",
    "CREATE INDEX IF NOT EXISTS conversation_messages_client "
    "ON conversation_messages(client_id, conversation_id, seq)",
    """CREATE TRIGGER IF NOT EXISTS conversation_messages_append_only_update
BEFORE UPDATE OF seq, client_id, conversation_id, message_id, role, content, attachments_json, views_json,
    created_at ON conversation_messages
BEGIN SELECT RAISE(ABORT, 'conversation messages are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS conversation_messages_append_only_delete BEFORE DELETE ON conversation_messages
WHEN EXISTS (SELECT 1 FROM clients WHERE id = OLD.client_id)
BEGIN SELECT RAISE(ABORT, 'conversation messages are append-only'); END""",
)
MESSAGE_STATUSES = frozenset({"stopped", "failed"})  # an unfinished turn's mark (NULL: finished)
CONVERSATION_LIMIT = 100  # messages of the current conversation loaded on startup
_ORDER_EVENTS = frozenset({"ticket", "checks", "confirm", "blocked", "request", "response", "status",
                           "cancel", "fill_posted", "live_acknowledged", "discarded", "nonce_rejected", "error",
                           "reply_blocked", "placed_manually", "reconciled", "withdrawn"})
_AUXILIARY = frozenset({"embeddings", "monitor", "ingest", "execution"})
# The base tables of a new database (version 3 without the ledger and contradictions).
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    confidence TEXT NOT NULL,
    expires_on TEXT,
    revision INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    UNIQUE (client_id, key, revision)
);
CREATE INDEX IF NOT EXISTS facts_client_key_revision
    ON facts(client_id, key, revision DESC);
CREATE TABLE IF NOT EXISTS batches (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resulting_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client_id, request_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    input_revision INTEGER NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    alternatives_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed','accepted','dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_client_created
    ON decisions(client_id, created_at, id);
CREATE TABLE IF NOT EXISTS decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('proposed','accepted','dismissed')),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_events_decision
    ON decision_events(decision_id, id);
CREATE TABLE IF NOT EXISTS auxiliary (
    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (client_id, namespace)
);
INSERT OR IGNORE INTO metadata(key, value)
    VALUES('schema_version', '3');
INSERT INTO decision_events(decision_id, status, recorded_at)
    SELECT d.id, 'proposed', d.created_at
    FROM decisions d
    WHERE NOT EXISTS (
        SELECT 1 FROM decision_events e WHERE e.decision_id = d.id
    );
INSERT INTO decision_events(decision_id, status, recorded_at)
    SELECT d.id, d.status, d.updated_at
    FROM decisions d
    WHERE d.status != 'proposed'
      AND NOT EXISTS (
        SELECT 1 FROM decision_events e
        WHERE e.decision_id = d.id AND e.status = d.status
    );
"""
# Candidate duplicates from a different statement: same account/kind/amount
# within this many days and similar descriptions.
DUPLICATE_WINDOW_DAYS = 3
DUPLICATE_SIMILARITY = 0.6
# Review horizons in days by key (exact) or prefix (ending in "."); the first match
# wins. A fact without an explicit expires_on is due for review this long after
# its observation. The horizon is a review deadline, not a prediction.
REVIEW_DAYS: tuple[tuple[str, int], ...] = (
    ("portfolio.snapshot", 30), ("household", 30), ("account.", 30), ("lot.", 30),
    ("liability.", 30), ("investment.", 90), ("cash.", 90), ("spending.", 90),
    ("analysis.", 30), ("plan.resources", 90), ("income.schedule", 90),
    ("planning.", 90), ("research.", 90), ("income.", 90), ("thesis.", 180), ("thread.", 180),
)
DEFAULT_REVIEW_DAYS = 365  # client.profile, goals, reserve, preference.*, constraint.*, tax.*, other
# The review horizons a version-1 store applied by default (everything else got 365 days).
_V1_REVIEW_DAYS: tuple[tuple[str, int], ...] = (
    ("portfolio.snapshot", 30), ("household", 30), ("account.", 30), ("lot.", 30),
    ("analysis.", 30), ("plan.resources", 90), ("income.schedule", 90),
    ("planning.", 90), ("research.", 90), ("thesis.", 180),
)
# Facts that set financial policy; document/web sources cannot establish them.
_POLICY_KEYS = frozenset({"goals", "client.profile", "tax.profile", "monitor.rules"})
_POLICY_PREFIXES = ("preference.", "constraint.")
_SENSITIVE_FIELDS = frozenset({
    "password", "passcode", "pin", "ssn", "social_security_number", "curp", "rfc",
    "account_number", "routing_number", "clabe", "card_number", "cvv", "api_key",
    "secret", "credential", "credentials", "token", "address", "street_address",
})
_SENSITIVE_PATTERNS = (
    ("a US Social Security number", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("a CURP", re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b")),
    ("an RFC", re.compile(r"\b[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}\b")),
    ("an account or card number", re.compile(r"\b\d(?:[ -]?\d){11,18}\b")),
)


class StoreError(ValueError):
    """Base class for expected store errors."""


class ValidationError(StoreError):
    """An input does not satisfy the store contract."""


class ClientNotFoundError(StoreError):
    """The requested client does not exist."""


class ClientExistsError(StoreError):
    """The requested client already exists."""


class StaleRevisionError(StoreError):
    """The expected revision is no longer current."""

    def __init__(self, expected: int, current: int):
        self.expected, self.current = expected, current
        super().__init__(
            f"stale revision: expected {expected}, current {current}; reload, "
            f"reconcile, and retry with expected_revision={current}"
        )


class RequestConflictError(StoreError):
    """An idempotency key was reused for a different request."""


class DecisionNotFoundError(StoreError):
    """The requested decision does not exist for this client."""


class ContradictionNotFoundError(ValidationError, LookupError):
    """The requested contradiction does not exist for this client."""


class IneligibleEvidenceError(StoreError):
    """Evidence cannot support the requested decision operation."""


def _exportable(namespace: str, value: Any) -> Any:
    """Auxiliary state as exported: order tickets never carry their confirmation nonce out."""
    if namespace != "execution" or not isinstance(value, dict):
        return value
    tickets = {tid: {k: v for k, v in ticket.items() if k not in ("nonce", "nonce_hash")}
               for tid, ticket in (value.get("tickets") or {}).items() if isinstance(ticket, dict)}
    return {**value, "tickets": tickets}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a nonempty string")
    return value


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("expected_revision must be a nonnegative integer")
    return value


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise ValidationError(f"{field} must be an ISO date (YYYY-MM-DD)")
    return parsed


def _validate_json(value: Any, field: str = "value") -> None:
    """Accept only strict, finite JSON values (and reject Python conveniences)."""

    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{field} contains a nonfinite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{field}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{field} contains a non-string object key")
            _validate_json(item, f"{field}.{key}")
        return
    raise ValidationError(f"{field} is not a JSON value")


def _json(value: Any) -> str:
    _validate_json(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def review_days(key: str, table: Sequence[tuple[str, int]] = REVIEW_DAYS) -> int:
    for pattern, days in table:
        if key == pattern or (pattern.endswith(".") and key.startswith(pattern)):
            return days
    return DEFAULT_REVIEW_DAYS


def _v1_review_days(key: str) -> int:
    return review_days(key, _V1_REVIEW_DAYS)


def is_stale(fact: Mapping[str, Any], today: date | None = None) -> bool:
    expires = fact.get("expires_on")
    return bool(expires) and expires < (today or _today()).isoformat()


def _is_policy_key(key: str) -> bool:
    return key in _POLICY_KEYS or key.startswith(_POLICY_PREFIXES)


def _sensitive(value: Any, field: str) -> str | None:
    if isinstance(value, str) and "://" not in value:
        for label, pattern in _SENSITIVE_PATTERNS:
            if pattern.search(value):
                return f"{field} looks like {label}"
    elif isinstance(value, list):
        return next(filter(None, (_sensitive(v, f"{field}[{i}]") for i, v in enumerate(value))), None)
    elif isinstance(value, dict):
        for name, item in value.items():
            if name.lower() in _SENSITIVE_FIELDS and item not in (None, ""):
                return f"{field}.{name} is an identifier, address, or credential field"
            found = _sensitive(item, f"{field}.{name}")
            if found:
                return found
    return None


def merge_patch(current: Any, patch: Any, field: str = "value") -> Any:
    """RFC 7386 merge for objects; lists of objects with ``id`` merge by id; other lists are replaced.

    A list of plain values (a thread's ``related`` keys) is replaced whole, as RFC 7386 does.
    """

    if isinstance(patch, dict):
        merged = dict(current) if isinstance(current, dict) else {}
        for name, item in patch.items():
            if item is None:
                merged.pop(name, None)
            else:
                merged[name] = merge_patch(merged.get(name), item, f"{field}.{name}")
        return merged
    if isinstance(patch, list) and isinstance(current, list):
        if not any(isinstance(i, dict) for i in (*current, *patch)):
            return patch
        if not all(isinstance(i, dict) and "id" in i for i in (*current, *patch)):
            raise ValidationError(
                f"merge of list {field} requires objects with an id; send the full list without merge"
            )
        merged = {item["id"]: item for item in current}
        for item in patch:
            merged[item["id"]] = merge_patch(merged.get(item["id"]), item, f"{field}[{item['id']}]")
        return list(merged.values())
    return patch


def _protected(row: Mapping[str, Any]) -> bool:
    """A value the person stated or confirmed themselves."""

    return row["source_kind"] == "user" and row["confidence"] in {"reported", "confirmed"}


def _same_record(fact: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """A write that would store exactly the current record again (the source's wording may differ)."""

    return (fact["value"] is not None and _json(fact["value"]) == row["value_json"]
            and fact["source"]["kind"] == row["source_kind"]
            and fact["source"]["observed_on"] == row["observed_on"]
            and fact["confidence"] == row["confidence"]
            and fact["valid_from"] == row["valid_from"]
            and fact["expires_on"] == row["expires_on"])


def _record_date(value: Any, fallback: Any) -> str | None:
    """The date a record describes: its ``as_of`` (a statement's closing date), else when it was valid from."""
    day = value.get("as_of") if isinstance(value, Mapping) else None
    day = day if isinstance(day, str) and len(day) >= 10 else fallback
    return str(day)[:10] if day else None


def _older_record(fact: Mapping[str, Any], prior: Mapping[str, Any]) -> tuple[str, str] | None:
    """(new date, saved date) when a settling record is older than the saved evidence it would replace.

    Only evidence against evidence: a statement never yields to what the person said, and an estimate
    is always replaced by a statement whatever the dates.
    """
    if prior["source_kind"] not in _EVIDENCE_KINDS:
        return None
    try:
        saved = json.loads(prior["value_json"])
    except (TypeError, ValueError):
        return None
    new_day = _record_date(fact.get("value"), fact.get("valid_from"))
    saved_day = _record_date(saved, prior["valid_from"])
    if new_day and saved_day and new_day < saved_day:
        return new_day, saved_day
    return None


def describe(value: Any) -> str:
    """A short readable rendering of a fact value (amount-shaped values as 'MXN 85,000')."""

    from .situation.text import fmt

    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return fmt(value)
    if isinstance(value, str):
        return value if len(value) <= 60 else value[:57] + "..."
    if isinstance(value, dict):
        if isinstance(value.get("statement"), dict) and value["statement"]:
            return " + ".join(f"{cur} {fmt(amount)}" for cur, amount in sorted(value["statement"].items()))
        for field in ("amount", "balance", "total", "value"):
            amount = value.get(field)
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                text = f"{value.get('currency') or ''} {fmt(amount)}".strip()
                period = value.get("frequency") or value.get("period")
                if period in {"monthly", "month"}:
                    text += " a month"
                elif period in {"annual", "year"}:
                    text += " a year"
                return text
    if isinstance(value, list):
        return f"{len(value)} item{'s' if len(value) != 1 else ''}"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 80 else text[:77] + "..."


_SOURCE_PHRASES = {
    "en": {"document": "a statement you shared", "web": "a web page", "connector": "your connected account",
           "inference": "my own reading", "pattern": "your transactions", "tool": "a calculation"},
    "es": {"document": "un estado de cuenta que compartiste", "web": "una página web",
           "connector": "tu cuenta conectada", "inference": "mi propia lectura", "pattern": "tus movimientos",
           "tool": "un cálculo"},
}
_INCOME_LABELS = {
    "en": {"salary": "your salary", "aguinaldo": "your aguinaldo", "ptu": "your profit sharing (PTU)",
           "bonus": "your bonus", "rent": "your rental income", "business": "your business income",
           "pension": "your pension"},
    "es": {"salary": "tu sueldo", "aguinaldo": "tu aguinaldo", "ptu": "tu reparto de utilidades (PTU)",
           "bonus": "tu bono", "rent": "tus ingresos por renta", "business": "los ingresos de tu negocio",
           "pension": "tu pensión"},
}
_DEBT_LABELS = {
    "en": {"auto": "car loan", "mortgage": "mortgage", "card": "credit card", "personal": "personal loan",
           "student": "student loan"},
    "es": {"auto": "crédito del coche", "mortgage": "hipoteca", "card": "tarjeta de crédito",
           "personal": "préstamo personal", "student": "crédito educativo"},
}
_MONTH_NAMES = {
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"),
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
           "noviembre", "diciembre"),
}


def _words(identifier: str) -> str:
    return identifier.replace("_", " ").replace("-", " ").strip()


def _label(key: str, value: Any, lang: str) -> str:
    """What a fact is, in the person's words: 'your salary', 'tus inversiones en GBM'."""

    es = lang == "es"
    head, _, rest = key.partition(".")
    value = value if isinstance(value, dict) else {}
    institution = value.get("institution") or value.get("lender")
    name = value.get("name") if isinstance(value.get("name"), str) else None
    if head == "income":
        known = _INCOME_LABELS[lang].get(value.get("kind") or rest)
        return known or (f"tu ingreso «{name or _words(rest)}»" if es else f"your income “{name or _words(rest)}”")
    if key == "spending.monthly":
        return "tu gasto mensual" if es else "your monthly spending"
    if head == "cash":
        if institution:
            return f"tu dinero en {institution}" if es else f"your cash at {institution}"
        return f"tu efectivo «{name or _words(rest)}»" if es else f"your cash “{name or _words(rest)}”"
    if head == "investment":
        if institution:
            return f"tus inversiones en {institution}" if es else f"your investments at {institution}"
        return f"tus inversiones «{name or _words(rest)}»" if es else f"your investments “{name or _words(rest)}”"
    if head == "liability":
        kind = _DEBT_LABELS[lang].get(value.get("kind"), "deuda" if es else "debt")
        where = f" con {institution}" if es and institution else (f" with {institution}" if institution else "")
        return f"tu {kind}{where}" if es else f"your {kind}{where}"
    if head == "account":
        account = value.get("account") if isinstance(value.get("account"), dict) else {}
        if account.get("institution"):
            return f"tu cuenta en {account['institution']}" if es else f"your account at {account['institution']}"
    return f"«{_words(rest or head)}»" if es else f"“{_words(rest or head)}”"


def _money(amount: Any, currency: Any, show_code: bool) -> str:
    from .situation.text import fmt

    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return "?"
    places = 0 if abs(amount) >= 1000 else None
    text = f"${fmt(round(amount) if places == 0 else amount, places)}"
    return text + (f" {currency}" if show_code and isinstance(currency, str) else "")


def _say(value: Any, lang: str, show_code: bool) -> str:
    """A fact value as a person would say it: '$85,000 al mes', '$38,601 USD'."""

    if isinstance(value, dict):
        statement = value.get("statement")
        if isinstance(statement, dict) and statement:
            return " + ".join(_money(amount, cur, show_code or len(statement) > 1)
                              for cur, amount in sorted(statement.items()))
        for field in ("amount", "balance", "total", "value"):
            amount = value.get(field)
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                text = _money(amount, value.get("currency"), show_code)
                period = value.get("frequency") or value.get("period")
                if period in {"monthly", "month"}:
                    text += " al mes" if lang == "es" else " a month"
                elif period in {"annual", "year"}:
                    text += " al año" if lang == "es" else " a year"
                return text
    return describe(value)


def _date(iso: Any, lang: str) -> str:
    try:
        day = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    month = _MONTH_NAMES[lang][day.month - 1]
    return f"{day.day} de {month} de {day.year}" if lang == "es" else f"{month} {day.day}, {day.year}"


def _currencies(*values: Any) -> set[str]:
    found = set()
    for value in values:
        if isinstance(value, dict):
            if isinstance(value.get("currency"), str):
                found.add(value["currency"])
            if isinstance(value.get("statement"), dict):
                found.update(value["statement"])
    return found


def _question(kind: str, key: str, current: Mapping[str, Any], proposed: Mapping[str, Any],
              language: str | None = None) -> str:
    """The question to put to the person, in their language and words (never raw keys)."""

    lang = "es" if str(language or "").lower().startswith("es") else "en"
    es = lang == "es"
    mine_value, new_value = current["value"], proposed["value"]
    show_code = len(_currencies(mine_value, new_value)) > 1
    about = isinstance(mine_value, dict) and mine_value.get("approximate") is True
    label = _label(key, mine_value, lang)
    mine = _say(mine_value, lang, show_code)
    source = proposed["source"]
    if kind == "stated_vs_statement":
        institution = proposed.get("institution") or (mine_value.get("institution") if isinstance(mine_value, dict) else None)
        where = (f" en {institution}" if es else f" at {institution}") if institution else ""
        day = _date(new_value.get("as_of") or source["observed_on"], lang)
        theirs = _say(new_value, lang, True if show_code else False)
        if es:
            return (f"Dijiste {'unos ' if about else ''}{mine}{where}, pero el estado de cuenta del {day} muestra "
                    f"{theirs}. ¿Mantengo tu cifra, uso la del estado de cuenta o cambió?")
        return (f"You said {'about ' if about else ''}{mine}{where}, but the statement from {day} shows {theirs}. "
                "Keep your figure, use the statement's, or did it change?")
    since = current.get("valid_from")
    where = _SOURCE_PHRASES[lang].get(source["kind"], source["kind"])
    day = _date(proposed.get("valid_from") or source["observed_on"], lang)
    theirs = _say(new_value, lang, show_code)
    if es:
        return (f"Me dijiste que {label} es {'de unos ' if about else 'de '}{mine}"
                + (f" (desde el {_date(since, lang)})" if since else "")
                + f", pero {where} indica {theirs} al {day}. ¿Mantengo la tuya, uso la nueva o cambió?")
    return (f"You told me {label} is {'about ' if about else ''}{mine}"
            + (f" (since {_date(since, lang)})" if since else "")
            + f", but {where} shows {theirs} as of {day}. Keep yours, use the new one, or did it change?")


def _period_text(fact: Mapping[str, Any]) -> str:
    """One line of a timeline: '85,000 since 2026-03', 'forgotten on 2026-09-21'."""

    start, end, status = fact.get("valid_from"), fact.get("valid_to"), fact.get("status")
    if status == "forgotten":
        return f"forgotten on {start}"
    if status == "replaced":
        return f"replaced by a statement from {start}"
    text = describe(fact.get("value"))
    if fact.get("confidence") == "inferred":
        text += " (inferred)"
    if status == "corrected":
        return f"{text} (corrected)"
    if not start:
        return text
    if end is None:
        return f"{text} since {start[:7]}"
    if end == start:
        return f"{text} (superseded)"
    return f"{text} from {start[:7]} to {end[:7]}"


def secure_delete(path: str | os.PathLike[str]) -> bool:
    """Overwrite a regular file with zeros, flush it to disk, then unlink it.

    A symbolic link is removed without touching its target.  Returns whether
    something was removed.  (On copy-on-write or journaling filesystems and SSDs
    an overwrite cannot guarantee the old blocks are gone; it still removes the
    plain copy the next reader would find.)
    """

    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        target.unlink()
        return True
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError:
        fd = None
    if fd is not None:
        try:
            remaining = info.st_size
            block = b"\0" * 65_536
            while remaining > 0:
                written = os.write(fd, block[:min(len(block), remaining)])
                remaining -= written
            os.fsync(fd)
        finally:
            os.close(fd)
    target.unlink()
    return True


def secure_delete_tree(root: str | os.PathLike[str]) -> int:
    """Securely delete every file under ``root`` (links are unlinked, never followed), then the directories."""

    base = Path(root)
    if base.is_symlink():
        base.unlink()
        return 0
    if not base.is_dir():
        return 0
    count = 0
    for current, directories, files in os.walk(base, topdown=False, followlinks=False):
        for name in files:
            count += secure_delete(Path(current) / name)
        for name in directories:
            path = Path(current) / name
            if path.is_symlink():
                path.unlink()
            else:
                path.rmdir()
    base.rmdir()
    return count


class WealthStore:
    """A local SQLite evidence store.

    ``delete_client`` removes this database's rows and securely deletes the
    client's upload directory (raw statements).  Copies previously exported by
    callers and external backups are outside that operation.
    """

    def __init__(self, path: str | os.PathLike[str]):
        if not isinstance(path, (str, os.PathLike)):
            raise ValidationError("path must be a filesystem path")
        self.path = os.fspath(path)
        if not self.path:
            raise ValidationError("path must not be empty")
        self._closed = False
        self._lock = threading.RLock()
        self._atomic_depth = 0
        self._savepoints: list[str] = []
        if self.path != ":memory:":
            db_path = Path(self.path).expanduser()
            db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(db_path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValidationError("database path must be a regular file")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            self.path = str(db_path)
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA secure_delete = ON")
        self._db.execute("PRAGMA journal_mode = DELETE")
        try:
            self._create_schema()
        except Exception:
            self._db.close()
            self._closed = True
            raise

    def __enter__(self) -> WealthStore:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError("store is closed")

    def _script(self, sql: str) -> None:
        """Run semicolon-separated DDL/DML inside the current transaction.

        (``executescript`` would commit first, so it cannot be used under a lock.)
        """

        for statement in sql.split(";"):
            if statement.strip():
                self._db.execute(statement)

    def _tables(self) -> set[str]:
        return {
            row["name"]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _schema_version(self, tables: set[str]) -> str | None:
        if "metadata" not in tables:
            return None
        row = self._db.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise StoreError("database metadata has no schema version")
        return row["value"]

    def _create_schema(self) -> None:
        """Create or migrate the schema; safe when several processes open one file at once.

        A current database is recognised without taking the write lock.  Anything
        else happens under ``BEGIN IMMEDIATE`` and re-reads the state first, so a
        process that waited for another's migration finds nothing left to do, and
        every step checks before it alters (idempotent).
        """

        with self._lock:
            tables = self._tables()
            if self._schema_version(tables) == str(SCHEMA_VERSION) and tables >= _TABLES | _LEDGER_TABLES:
                if not self._has_orders():
                    self._migrate_orders()
                if not self._has_market():
                    self._migrate_market()
                if not self._has_conversations() or not self._has_message_status():
                    self._migrate_conversations()
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._create_or_migrate()
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            self._migrate_orders()
            self._migrate_market()
            self._migrate_conversations()

    def _has_conversations(self) -> bool:
        return self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'conversation_messages_append_only_delete'"
        ).fetchone() is not None

    def _has_message_status(self) -> bool:
        return any(row[1] == "status" for row in self._db.execute("PRAGMA table_info(conversation_messages)"))

    def _migrate_conversations(self) -> None:
        """Add the chat conversation tables (idempotent and additive; no version change)."""

        self._db.execute("BEGIN IMMEDIATE")
        try:
            if not self._has_conversations():
                for statement in _CONVERSATION_SCHEMA:
                    self._db.execute(statement)
            if not self._has_message_status():
                self._db.execute("ALTER TABLE conversation_messages ADD COLUMN status TEXT")
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _has_market(self) -> bool:
        return self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'market_fetches_symbol'"
        ).fetchone() is not None

    def _migrate_market(self) -> None:
        """Add the market-data cache tables (idempotent and additive; no version change)."""

        self._db.execute("BEGIN IMMEDIATE")
        try:
            if not self._has_market():
                for statement in _MARKET_SCHEMA:
                    self._db.execute(statement)
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _has_orders(self) -> bool:
        return self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'orders_append_only_delete'"
        ).fetchone() is not None

    def _migrate_orders(self) -> None:
        """Add the append-only ``orders`` audit table (idempotent and additive; no version change)."""

        self._db.execute("BEGIN IMMEDIATE")
        try:
            if not self._has_orders():
                for statement in _ORDERS_SCHEMA:
                    self._db.execute(statement)
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _create_or_migrate(self) -> None:
        tables = self._tables()
        version = self._schema_version(tables)
        if version is None and tables:
            raise StoreError("existing database has no wealth schema version")
        if version is not None:
            base = _TABLES - {"contradictions"}
            migrated_from = version
            if version == "1" and tables >= base:
                self._migrate_to_2()
                version = "2"
            if version == "2" and tables >= base:
                self._migrate_to_3(recompute_review=migrated_from == "1")
                return
            if version != str(SCHEMA_VERSION):
                raise StoreError(f"unsupported schema version {version}; expected {SCHEMA_VERSION}")
            if tables >= _TABLES | _LEDGER_TABLES:
                return
        self._script(_BASE_SCHEMA + _LEDGER_SCHEMA + _CONTRADICTION_SCHEMA)

    def _migrate_to_2(self) -> None:
        """Add ledger tables to a version-1 database (inside the caller's transaction)."""

        self._script(_LEDGER_SCHEMA + "UPDATE metadata SET value = '2' WHERE key = 'schema_version';")

    def _migrate_to_3(self, recompute_review: bool = False) -> None:
        """Add valid time and contradictions to a version-2 database (inside the caller's transaction).

        Every existing revision becomes valid from its observation date and is
        closed where the next revision of the same key begins.  Old null
        retractions become ``forgotten`` tombstones.  Facts written by a
        version-1 store got a 365-day review date for most keys; a date that was
        that default is recomputed from ``REVIEW_DAYS`` so migrated facts go
        stale when the same fact written today would.
        """

        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(facts)")}
        added = {"valid_from": "TEXT", "valid_to": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'"}
        for name, kind in added.items():
            if name not in columns:
                self._db.execute(f"ALTER TABLE facts ADD COLUMN {name} {kind}")
        self._script(
            """
            UPDATE facts SET valid_from = observed_on;
            UPDATE facts SET status = 'forgotten' WHERE value_json = 'null';
            UPDATE facts SET valid_to = (
                SELECT MAX(n.valid_from, facts.valid_from) FROM facts n
                WHERE n.client_id = facts.client_id AND n.key = facts.key
                  AND n.revision > facts.revision
                ORDER BY n.revision LIMIT 1
            );
            UPDATE facts SET valid_to = valid_from WHERE status = 'forgotten';
            """
            + _CONTRADICTION_SCHEMA
        )
        if recompute_review:
            for row in self._db.execute("SELECT id, key, observed_on, expires_on FROM facts").fetchall():
                try:
                    observed = date.fromisoformat(row["observed_on"])
                except (TypeError, ValueError):
                    continue
                default = (observed + timedelta(days=_v1_review_days(row["key"]))).isoformat()
                current = (observed + timedelta(days=review_days(row["key"]))).isoformat()
                if row["expires_on"] == default and current != default:
                    self._db.execute("UPDATE facts SET expires_on = ? WHERE id = ?", (current, row["id"]))
        self._db.execute("UPDATE metadata SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))

    def _begin(self) -> None:
        """Start a write: a transaction, or a savepoint inside ``atomic``."""

        self._ensure_open()
        if self._atomic_depth:
            name = f"wealth_{len(self._savepoints) + 1}"
            self._db.execute(f"SAVEPOINT {name}")
            self._savepoints.append(name)
        else:
            self._db.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        if self._savepoints:
            self._db.execute(f"RELEASE {self._savepoints.pop()}")
        else:
            self._db.execute("COMMIT")

    def _rollback(self) -> None:
        if self._savepoints:
            name = self._savepoints.pop()
            self._db.execute(f"ROLLBACK TO {name}")
            self._db.execute(f"RELEASE {name}")
        elif self._db.in_transaction:
            self._db.execute("ROLLBACK")

    @contextmanager
    def atomic(self) -> Iterator[WealthStore]:
        """Run several store operations as one write transaction (all or nothing).

        The write lock is taken up front (``BEGIN IMMEDIATE``), so what is read
        inside cannot change underneath: other processes wait, then see either
        none or all of the writes.  Nested calls join the outer transaction.
        """

        with self._lock:
            if self._atomic_depth:
                self._atomic_depth += 1
                try:
                    yield self
                finally:
                    self._atomic_depth -= 1
                return
            self._begin()
            self._atomic_depth = 1
            try:
                yield self
            except BaseException:
                self._atomic_depth = 0
                self._savepoints.clear()
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            else:
                self._atomic_depth = 0
                self._savepoints.clear()
                self._db.execute("COMMIT")

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        """Hold one SQLite snapshot across a multi-query read."""

        self._ensure_open()
        owns_transaction = not self._db.in_transaction
        if owns_transaction:
            self._db.execute("BEGIN")
        try:
            yield
        except Exception:
            if owns_transaction and self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        else:
            if owns_transaction and self._db.in_transaction:
                self._db.execute("COMMIT")

    def _client_row(self, client_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT id, display_name, revision, created_at FROM clients WHERE id = ?",
            (client_id,),
        ).fetchone()
        if row is None:
            raise ClientNotFoundError(f"client {client_id!r} does not exist")
        return row

    def _check_revision(self, client_id: str, expected_revision: int | None) -> sqlite3.Row:
        row = self._client_row(client_id)
        if expected_revision is not None and row["revision"] != expected_revision:
            raise StaleRevisionError(expected_revision, row["revision"])
        return row

    def create_client(self, client_id: str, display_name: str) -> dict[str, Any]:
        client_id = _required_text(client_id, "client_id")
        display_name = _required_text(display_name, "display_name")
        now = _utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._db.execute(
                    "INSERT INTO clients(id, display_name, revision, created_at) "
                    "VALUES (?, ?, 0, ?)",
                    (client_id, display_name, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ClientExistsError(f"client {client_id!r} already exists") from exc
        return {"id": client_id, "display_name": display_name, "revision": 0}

    def _fact_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "key": row["key"],
            "value": json.loads(row["value_json"]),
            "source": {
                "kind": row["source_kind"],
                "ref": row["source_ref"],
                "observed_on": row["observed_on"],
            },
            "confidence": row["confidence"],
            "expires_on": row["expires_on"],
            "revision": row["revision"],
            "recorded_at": row["recorded_at"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "status": row["status"],
        }

    def _current_rows(
        self, client_id: str, keys: Sequence[str] | None = None
    ) -> dict[str, sqlite3.Row]:
        """The latest revision of each key, tombstones included (check ``status``)."""

        where = f" AND key IN ({','.join('?' for _ in keys)})" if keys else ""
        return {
            row["key"]: row
            for row in self._db.execute(
                "SELECT f.* FROM facts f JOIN (SELECT key, MAX(revision) revision FROM facts "
                f"WHERE client_id = ?{where} GROUP BY key) latest "
                "ON latest.key = f.key AND latest.revision = f.revision "
                "WHERE f.client_id = ? ORDER BY f.key",
                (client_id, *(keys or ()), client_id),
            )
        }

    def _active_facts(self, client_id: str) -> list[dict[str, Any]]:
        return [
            self._fact_from_row(row)
            for row in self._current_rows(client_id).values()
            if row["status"] == "active"
        ]

    def _latest_ids(self, client_id: str) -> dict[str, tuple[str, int]]:
        return {
            row["key"]: (row["id"], row["revision"])
            for row in self._db.execute(
                "SELECT f.key, f.id, f.revision FROM facts f JOIN "
                "(SELECT key, MAX(revision) revision FROM facts "
                " WHERE client_id = ? GROUP BY key) latest "
                "ON latest.key = f.key AND latest.revision = f.revision "
                "WHERE f.client_id = ?",
                (client_id, client_id),
            )
        }

    def _evidence_problems(
        self,
        client_id: str,
        evidence_ids: Sequence[str],
        latest: Mapping[str, tuple[str, int]] | None = None,
    ) -> list[str]:
        """Name each cited fact that no longer supports a decision."""

        if not evidence_ids:
            return ["no evidence is cited"]
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = {
            row["id"]: row
            for row in self._db.execute(
                f"SELECT id, key, confidence, expires_on FROM facts "
                f"WHERE client_id = ? AND id IN ({placeholders})",
                (client_id, *evidence_ids),
            )
        }
        latest = self._latest_ids(client_id) if latest is None else latest
        today = _today().isoformat()
        problems = []
        for evidence_id in evidence_ids:
            row = rows.get(evidence_id)
            if row is None:
                problems.append(f"evidence {evidence_id} does not belong to this client")
                continue
            key = row["key"]
            current_id, current_revision = latest.get(key, (None, None))
            if current_id != evidence_id:
                problems.append(
                    f"{key} changed at revision {current_revision} (cited evidence {evidence_id})"
                )
            elif row["confidence"] == "inferred":
                problems.append(f"{key} is inferred (evidence {evidence_id}); confirm it with the user")
            elif row["expires_on"] and row["expires_on"] < today:
                problems.append(
                    f"{key} passed its review date {row['expires_on']} (evidence {evidence_id}); "
                    "reconfirm it with the user"
                )
        return problems

    def _decision_from_row(
        self, row: sqlite3.Row, latest: Mapping[str, tuple[str, int]] | None = None
    ) -> dict[str, Any]:
        problems = self._evidence_problems(
            row["client_id"], json.loads(row["evidence_ids_json"]), latest
        )
        return {
            "id": row["id"],
            "title": row["title"],
            "rationale": row["rationale"],
            "revision": row["input_revision"],
            "evidence_ids": json.loads(row["evidence_ids_json"]),
            "alternatives": json.loads(row["alternatives_json"]),
            "status": row["status"],
            "needs_review": bool(problems),
            "review_reasons": problems,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _decision_events(self, decision_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, status, recorded_at FROM decision_events "
            "WHERE decision_id = ? ORDER BY id",
            (decision_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def snapshot(self, client_id: str) -> dict[str, Any]:
        client_id = _required_text(client_id, "client_id")
        with self._lock:
            with self._read_transaction():
                client = self._client_row(client_id)
                facts = self._active_facts(client_id)
                decisions = self._db.execute(
                    "SELECT * FROM decisions WHERE client_id = ? ORDER BY created_at, id",
                    (client_id,),
                ).fetchall()
                latest = self._latest_ids(client_id) if decisions else {}
                snapshot = {
                    "client": {
                        "id": client["id"],
                        "display_name": client["display_name"],
                        "revision": client["revision"],
                    },
                    "facts": facts,
                    "decisions": [self._decision_from_row(row, latest) for row in decisions],
                }
                kept = self._kept_statements(client_id)
                if kept:
                    # Stated balances the person kept over a statement; the situation honours them.
                    snapshot["kept"] = kept
                return snapshot

    def _kept_statements(self, client_id: str) -> list[dict[str, Any]]:
        try:
            rows = self._db.execute(
                "SELECT key, proposed_key, current_fact_id, proposed_json FROM contradictions "
                "WHERE client_id = ? AND kind = 'stated_vs_statement' AND status = 'kept' ORDER BY created_at, id",
                (client_id,),
            ).fetchall()
        except sqlite3.OperationalError:  # a database from before contradictions existed
            return []
        return [{"key": row["key"], "proposed_key": row["proposed_key"], "current_fact_id": row["current_fact_id"],
                 "as_of": (json.loads(row["proposed_json"]).get("value") or {}).get("as_of")} for row in rows]

    def _normalize_fact(self, raw: Any) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(raw, Mapping):
            raise ValidationError("each fact must be an object")
        if any(not isinstance(field, str) for field in raw):
            raise ValidationError("fact field names must be strings")
        allowed = {"key", "value", "source", "confidence", "expires_on", "merge", "valid_from"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(f"unknown fact fields: {sorted(unknown)!r}; allowed {sorted(allowed)!r}")
        key = _required_text(raw.get("key"), "fact.key")
        if "value" not in raw:
            raise ValidationError(f"fact {key!r} requires value")
        value = raw["value"]
        _validate_json(value, f"{key}.value")
        too_large = out_of_range(value, f"{key}.value")
        if too_large:
            raise ValidationError(too_large)
        merge = raw.get("merge", False)
        if not isinstance(merge, bool):
            raise ValidationError(f"{key}.merge must be true or false")
        source = raw.get("source")
        if not isinstance(source, Mapping) or set(source) != {"kind", "ref", "observed_on"}:
            raise ValidationError(
                f"{key}.source must be an object with exactly kind, ref, and observed_on"
            )
        kind = source.get("kind")
        if not isinstance(kind, str) or kind not in _SOURCE_KINDS:
            raise ValidationError(f"{key}.source.kind must be one of {sorted(_SOURCE_KINDS)!r}")
        ref = _required_text(source.get("ref"), f"{key}.source.ref")
        if kind == "web" and not ref.startswith(("https://", "http://")):
            raise ValidationError(f"{key}.source.ref must be the page URL for a web source")
        observed = _iso_date(source.get("observed_on"), f"{key}.source.observed_on")
        # The earliest local date anywhere (UTC+14) is at most one day past UTC.
        if observed > _today() + timedelta(days=1):
            raise ValidationError(f"{key}.source.observed_on must not be in the future")
        if kind != "tool":
            found = _sensitive(value, f"{key}.value")
            if found:
                raise ValidationError(
                    f"{found}; do not store government IDs, account numbers, addresses, or credentials"
                )
        confidence = raw.get("confidence", "reported")
        if not isinstance(confidence, str) or confidence not in _CONFIDENCES:
            raise ValidationError(f"{key}.confidence must be one of {sorted(_CONFIDENCES)!r}")
        if confidence == "confirmed" and kind != "user":
            raise ValidationError(f"{key}: only a user source can carry confirmed confidence")
        if kind in {"inference", "pattern"} and confidence != "inferred":
            raise ValidationError(
                f"{key}: an {kind} source must carry inferred confidence until the person confirms it"
            )
        if raw.get("valid_from") is None:
            valid_from = observed
        else:
            valid_from = _iso_date(raw["valid_from"], f"{key}.valid_from")
            if valid_from > _today() + timedelta(days=1):
                raise ValidationError(f"{key}.valid_from must not be in the future")
        warnings = []
        if kind in {"document", "web", "connector"} and _is_policy_key(key) and confidence != "inferred":
            confidence = "inferred"
            warnings.append(
                f"{key} came from a {kind} source, so it was saved as inferred; ask the "
                "user to confirm it, then save their answer with source.kind=user"
            )
        expires_text = raw.get("expires_on")
        if expires_text is None:
            expires = observed + timedelta(days=review_days(key))
        else:
            expires = _iso_date(expires_text, f"{key}.expires_on")
            if expires < observed:
                raise ValidationError(f"{key}.expires_on must not precede source.observed_on")
        if expires < _today():
            warnings.append(
                f"{key} was observed on {observed.isoformat()} and is already past its "
                f"review date {expires.isoformat()}; reconfirm it with the user"
            )
        return {
            "key": key,
            "value": value,
            "source": {"kind": kind, "ref": ref, "observed_on": observed.isoformat()},
            "confidence": confidence,
            "expires_on": expires.isoformat(),
            "merge": merge,
            "valid_from": valid_from.isoformat(),
        }, warnings

    def _action(self, row: sqlite3.Row) -> str:
        """How a written revision relates to the one before it (derived, so replays agree)."""

        if row["status"] in _TERMINAL_STATUSES:
            return "forget" if row["status"] == "forgotten" else "replace"
        prior = self._db.execute(
            "SELECT confidence, status FROM facts WHERE client_id = ? AND key = ? AND revision < ? "
            "ORDER BY revision DESC LIMIT 1",
            (row["client_id"], row["key"], row["revision"]),
        ).fetchone()
        if prior is None or prior["status"] in _TERMINAL_STATUSES:
            return "add"
        if prior["confidence"] == "inferred" and row["confidence"] != "inferred":
            return "supersede"
        return "update"

    def _receipt(
        self, client_id: str, revision: int | None, request_id: str | None, replayed: bool,
        warnings: list[str], needs_user: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        client = self._client_row(client_id)
        written = [] if revision is None else self._db.execute(
            "SELECT * FROM facts WHERE client_id = ? AND revision = ? ORDER BY key",
            (client_id, revision),
        ).fetchall()
        if needs_user is None:
            needs_user = [] if request_id is None else [
                self._contradiction_from_row(row) for row in self._db.execute(
                    "SELECT * FROM contradictions WHERE client_id = ? AND request_id = ? "
                    "AND status = 'pending' ORDER BY created_at, id",
                    (client_id, request_id),
                )
            ]
        return {
            "client": {"id": client_id, "revision": client["revision"]},
            "written": [
                {"key": row["key"], "id": row["id"], "confidence": row["confidence"],
                 "expires_on": row["expires_on"], "source_kind": row["source_kind"],
                 "valid_from": row["valid_from"], "action": self._action(row)}
                for row in written
            ],
            "needs_user": needs_user,
            "write_result": {
                "request_id": request_id,
                "resulting_revision": client["revision"] if revision is None else revision,
                "replayed": replayed,
            },
            "warnings": warnings,
        }

    def remember(
        self,
        client_id: str,
        facts: Sequence[Mapping[str, Any]],
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Write facts atomically and return a compact receipt.

        Without ``expected_revision`` a write may add new keys or ``merge`` into
        existing ones inside the write transaction; replacing an existing key
        requires the revision the caller read, unless the current value is
        inferred and the new one is better evidence.

        Time: each revision is valid from ``valid_from`` (default: the source's
        observation date).  A new value closes the previous revision at that
        date instead of erasing it.  A ``null`` value forgets the key: the
        history keeps a ``forgotten`` tombstone and the value is no longer used.

        Update policy: a document, web, connector, inference or pattern source
        never overwrites a value the person stated or confirmed.  The write is
        held as a pending contradiction and returned in ``needs_user``; the
        person decides (``resolve_contradiction``).  A statement saved under
        ``account.*`` that disagrees with a stated ``investment.*``/``cash.*``
        balance opens the same kind of contradiction.
        """

        client_id = _required_text(client_id, "client_id")
        if expected_revision is not None:
            expected_revision = _revision(expected_revision)
        if not isinstance(facts, list) or not facts:
            raise ValidationError("facts must be a nonempty list")
        normalized, warnings = [], []
        for raw in facts:
            fact, notes = self._normalize_fact(raw)
            normalized.append(fact)
            warnings.extend(notes)
        keys = [fact["key"] for fact in normalized]
        if len(keys) != len(set(keys)):
            raise ValidationError("a write cannot contain duplicate fact keys")
        if request_id is not None:
            request_id = _required_text(request_id, "request_id")
        payload = {"expected_revision": expected_revision, "facts": normalized}
        payload_hash = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                client = self._client_row(client_id)
                if request_id is not None:
                    prior = self._db.execute(
                        "SELECT payload_hash, resulting_revision FROM batches "
                        "WHERE client_id = ? AND request_id = ?",
                        (client_id, request_id),
                    ).fetchone()
                    if prior is not None:
                        if prior["payload_hash"] != payload_hash:
                            raise RequestConflictError(
                                f"request_id {request_id!r} was reused with a different payload; "
                                "use a new request_id for a new write"
                            )
                        result = self._receipt(
                            client_id, prior["resulting_revision"], request_id, True, []
                        )
                        self._commit()
                        return result
                current_revision = client["revision"]
                if expected_revision is not None and current_revision != expected_revision:
                    raise StaleRevisionError(expected_revision, current_revision)
                current = {
                    key: row for key, row in self._current_rows(client_id, keys).items()
                    if row["status"] == "active"
                }
                needs_user: list[dict[str, Any]] = []
                unchanged: list[str] = []
                to_write: list[tuple[dict[str, Any], sqlite3.Row | None]] = []
                history: list[dict[str, Any]] = []
                for fact in normalized:
                    key = fact["key"]
                    prior_fact = current.get(key)
                    if fact["merge"] and prior_fact is not None and fact["value"] is not None:
                        fact["value"] = merge_patch(
                            json.loads(prior_fact["value_json"]), fact["value"], key
                        )
                    elif fact["merge"] and isinstance(fact["value"], dict):
                        fact["value"] = merge_patch({}, fact["value"])
                    if prior_fact is not None and _same_record(fact, prior_fact):
                        # A retried or repeated write (same value, source kind, confidence and dates): nothing
                        # changes, so it is not a new revision and never an error.
                        unchanged.append(key)
                        continue
                    if (
                        prior_fact is not None
                        and _protected(prior_fact)
                        and fact["source"]["kind"] in _CHALLENGER_KINDS
                        and _json(fact["value"]) != prior_fact["value_json"]
                        # A statement, payslip or connected account is the source of truth for the figures
                        # it covers: it replaces what the person estimated (history keeps the estimate).
                        and not (fact["source"]["kind"] in _EVIDENCE_KINDS and key.startswith(_FIGURE_PREFIXES))
                    ):
                        opened = self._open_contradiction(
                            client_id, "same_key", key, key, prior_fact,
                            {name: fact[name] for name in
                             ("value", "source", "confidence", "expires_on", "valid_from")},
                            request_id, now,
                        )
                        if opened is None:
                            warnings.append(
                                f"{key} was not changed: the person already chose to keep their "
                                "value over this same figure"
                            )
                        else:
                            needs_user.append(opened)
                            warnings.append(
                                f"{key} was not changed: the person told us something different. "
                                f"Ask them (needs_user {opened['id']}); never pick a side silently"
                            )
                        continue
                    settles = fact["source"]["kind"] in _EVIDENCE_KINDS and key.startswith(_FIGURE_PREFIXES)
                    if settles and prior_fact is not None and fact["value"] is not None:
                        older = _older_record(fact, prior_fact)
                        if older:
                            # An older statement re-uploaded never replaces a newer one: it becomes history.
                            history.append((fact, older))
                            continue
                        if _protected(prior_fact) and not key.startswith("account.") \
                                and _json(fact["value"]) != prior_fact["value_json"]:
                            warnings.append(f"{key} was updated from a {fact['source']['kind']} "
                                            f"({fact['source']['ref']}); the person had said something else, so "
                                            "mention the new figure once")
                    if prior_fact is not None and not fact["merge"] and expected_revision is None and not settles:
                        if not (prior_fact["confidence"] == "inferred"
                                and fact["confidence"] != "inferred"):
                            raise ValidationError(
                                f"{key} already has a value (client revision {current_revision}); send it with "
                                f"merge=true and only the changed fields (no expected_revision needed), or pass "
                                f"expected_revision={current_revision} to replace it wholesale"
                            )
                    if (fact["merge"] and prior_fact is not None
                            and (prior_fact["confidence"] == "inferred" or is_stale(dict(prior_fact)))):
                        warnings.append(
                            f"{key} was merged into an inferred or past-review value "
                            f"observed on {prior_fact['observed_on']}; reconfirm the unchanged parts"
                        )
                    if fact["value"] is not None:
                        try:
                            warnings.extend(validate_canonical(key, fact["value"]))
                        except SchemaError as exc:
                            hint = "" if prior_fact is not None else self._existing_keys_hint(client_id, key)
                            raise ValidationError(f"{exc}; see fact_contract.schema{hint}") from exc
                    to_write.append((fact, prior_fact))
                new_revision = None
                if to_write:
                    new_revision = current_revision + 1
                    self._db.execute(
                        "UPDATE clients SET revision = ? WHERE id = ?", (new_revision, client_id)
                    )
                    for fact, prior_fact in to_write:
                        forgotten = fact["value"] is None
                        self._insert_fact(
                            client_id, fact, new_revision, now,
                            status="forgotten" if forgotten else "active",
                        )
                        if prior_fact is not None:
                            self._close(prior_fact["id"], fact["valid_from"])
                    statement_keys = sorted(
                        fact["key"] for fact, _ in to_write
                        if fact["key"].startswith("account.") and fact["key"].count(".") == 1
                        and fact["source"]["kind"] in _CHALLENGER_KINDS and fact["value"] is not None
                    )
                    for replaced in (self._statement_contradictions(client_id, statement_keys, request_id, now)
                                     if statement_keys else []):
                        warnings.append(
                            f"the statement in {replaced['replaced_by']} replaced the estimate in {replaced['key']} "
                            f"({replaced['stated']} stated, {replaced['statement']} on the statement); mention the "
                            "difference once if it matters, do not ask about it"
                        )
                for fact, (new_day, saved_day) in history:
                    kept = self._insert_history(client_id, fact, current[fact["key"]], now)
                    warnings.append(f"{fact['key']}: this record is dated {new_day}, older than the saved one "
                                    f"({saved_day}); " + ("it was kept in history and " if kept else "")
                                    + "the newer figure stays current")
                if request_id is not None:
                    self._db.execute(
                        "INSERT INTO batches(client_id, request_id, payload_hash, "
                        "resulting_revision, created_at) VALUES (?, ?, ?, ?, ?)",
                        (client_id, request_id, payload_hash,
                         current_revision if new_revision is None else new_revision, now),
                    )
                result = self._receipt(
                    client_id, new_revision, request_id, False, warnings, needs_user
                )
                if unchanged:
                    result["unchanged"] = unchanged
                self._commit()
            except Exception:
                self._rollback()
                raise
        return result

    def _existing_keys_hint(self, client_id: str, key: str) -> str:
        """For a new key that failed the schema: the saved keys of its kind, in case an update was meant."""
        head, dot, _ = key.partition(".")
        if not dot or key in {"client.profile", "spending.monthly", "preference.risk"}:
            return ""
        rows = self._db.execute(
            "SELECT DISTINCT key FROM facts WHERE client_id = ? AND key LIKE ? ESCAPE '\\' "
            "AND status = 'active' AND valid_to IS NULL ORDER BY key LIMIT 12",
            (client_id, head.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"),
        ).fetchall()
        saved = [row[0] for row in rows if row[0] != key]
        if not saved:
            return ""
        return (f". {key} is a new key; saved {head} keys: {', '.join(saved)}. To update one of those, "
                "send its exact key with merge=true and only the changed fields")

    def _insert_fact(
        self, client_id: str, fact: Mapping[str, Any], revision: int, now: str, *,
        status: str = "active",
    ) -> str:
        fact_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO facts(id, client_id, key, value_json, source_kind, source_ref, "
            "observed_on, confidence, expires_on, revision, recorded_at, valid_from, valid_to, "
            "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact_id, client_id, fact["key"], _json(fact["value"]),
                fact["source"]["kind"], fact["source"]["ref"], fact["source"]["observed_on"],
                fact["confidence"], fact.get("expires_on"), revision, now, fact["valid_from"],
                fact["valid_from"] if status in _TERMINAL_STATUSES else None, status,
            ),
        )
        return fact_id

    def _insert_history(self, client_id: str, fact: Mapping[str, Any], newer: sqlite3.Row, now: str) -> bool:
        """Save an older record behind the current one: closed when the newer one begins, never current.

        It takes the highest revision below the current row that this key has not used, so the timeline
        (ordered by revision) reads oldest first and the newer record stays the latest revision.
        """
        used = {row[0] for row in self._db.execute(
            "SELECT revision FROM facts WHERE client_id = ? AND key = ?", (client_id, fact["key"]))}
        revision = next((r for r in range(newer["revision"] - 1, -1, -1) if r not in used), None)
        if revision is None:
            return False  # no slot before the current record; the newer figure stays and nothing older is added
        self._db.execute(
            "INSERT INTO facts(id, client_id, key, value_json, source_kind, source_ref, observed_on, confidence, "
            "expires_on, revision, recorded_at, valid_from, valid_to, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (uuid.uuid4().hex, client_id, fact["key"], _json(fact["value"]), fact["source"]["kind"],
             fact["source"]["ref"], fact["source"]["observed_on"], fact["confidence"], fact.get("expires_on"),
             revision, now, fact["valid_from"], max(str(newer["valid_from"] or fact["valid_from"]), str(fact["valid_from"]))),
        )
        return True

    def _close(self, fact_id: str, at: str, *, corrected: bool = False) -> None:
        """End a revision's valid time (never before it began); the value is kept."""

        if corrected:
            self._db.execute(
                "UPDATE facts SET status = 'corrected', valid_to = valid_from WHERE id = ?",
                (fact_id,),
            )
        else:
            self._db.execute(
                "UPDATE facts SET valid_to = MAX(?, valid_from) WHERE id = ? AND valid_to IS NULL",
                (at, fact_id),
            )

    # -- contradictions ------------------------------------------------------

    def _contradiction_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        current = json.loads(row["current_json"])
        proposed = json.loads(row["proposed_json"])
        return {
            "id": row["id"],
            "kind": row["kind"],
            "key": row["key"],
            "proposed_key": row["proposed_key"],
            "current_value": current["value"],
            "proposed_value": proposed["value"],
            "sources": {"current": current["source"], "proposed": proposed["source"]},
            "valid_from": {"current": current.get("valid_from"), "proposed": proposed.get("valid_from")},
            "question": row["question"],
            "choices": list(_CONTRADICTION_CHOICES),
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolution": json.loads(row["resolution_json"]) if row["resolution_json"] else None,
        }

    def _language(self, client_id: str) -> str | None:
        """The person's language from client.profile (questions are worded in it)."""

        row = self._current_rows(client_id, ["client.profile"]).get("client.profile")
        if row is None or row["status"] != "active":
            return None
        value = json.loads(row["value_json"])
        if not isinstance(value, dict):
            return None
        language = value.get("language") or value.get("locale")
        return language if isinstance(language, str) else None

    def _open_contradiction(
        self, client_id: str, kind: str, key: str, proposed_key: str, current: sqlite3.Row,
        proposed: Mapping[str, Any], request_id: str | None, now: str,
    ) -> dict[str, Any] | None:
        """Record (or reuse) a pending question; None when the person already kept theirs."""

        earlier = self._db.execute(
            "SELECT * FROM contradictions WHERE client_id = ? AND key = ? AND proposed_key = ? "
            "AND status IN ('pending', 'kept') ORDER BY created_at DESC, id",
            (client_id, key, proposed_key),
        ).fetchall()
        for row in earlier:
            same = (row["current_fact_id"] == current["id"]
                    and json.loads(row["proposed_json"])["value"] == proposed["value"])
            if same:
                return self._contradiction_from_row(row) if row["status"] == "pending" else None
        self._db.execute(
            "UPDATE contradictions SET status = 'replaced', resolved_at = ? "
            "WHERE client_id = ? AND key = ? AND proposed_key = ? AND status = 'pending'",
            (now, client_id, key, proposed_key),
        )
        current_part = {
            "fact_id": current["id"], "value": json.loads(current["value_json"]),
            "source": {"kind": current["source_kind"], "ref": current["source_ref"],
                       "observed_on": current["observed_on"]},
            "confidence": current["confidence"], "valid_from": current["valid_from"],
        }
        question = _question(kind, key, current_part, proposed, self._language(client_id))
        contradiction_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO contradictions(id, client_id, kind, key, proposed_key, current_fact_id, "
            "current_json, proposed_json, question, status, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (contradiction_id, client_id, kind, key, proposed_key, current["id"],
             _json(current_part), _json(dict(proposed)), question, request_id, now),
        )
        return self._contradiction_from_row(self._db.execute(
            "SELECT * FROM contradictions WHERE id = ?", (contradiction_id,)
        ).fetchone())

    def _statement_contradictions(
        self, client_id: str, statement_keys: Sequence[str], request_id: str | None, now: str,
    ) -> list[dict[str, Any]]:
        """Replace stated balances that a just-saved statement covers; returns what was replaced."""

        from .situation.model import build

        rows = {r["key"]: r for r in self._current_rows(client_id).values() if r["status"] == "active"}
        picture = build({"facts": [self._fact_from_row(r) for r in rows.values()]}, None, _today())
        opened = []
        for difference in picture["differences"]:
            stated_key = difference.get("key") or ""
            stated_row = rows.get(stated_key)
            if (difference.get("kind") == "cash" or stated_row is None or not _protected(stated_row)
                    or not stated_key.startswith(("investment.", "cash."))):
                continue
            # Only the statement accounts the picture itself matched to this stated balance (institution, or a
            # name naming it, with a compatible kind): a bank statement never replaces a fund or an AFORE.
            matching = sorted(k for k in difference.get("accounts") or [] if k in statement_keys)
            gap, value = difference.get("difference"), difference.get("statement_value")
            if not matching or gap is None or value is None:
                continue
            stated_value = value - gap
            tolerance = STATED_TOLERANCE_APPROXIMATE if difference.get("stated_approximate") else STATED_TOLERANCE
            if abs(gap) <= abs(stated_value) * tolerance:
                continue
            account = rows[matching[0]]
            proposed = {
                "value": {"statement": difference.get("statement"), "value": value,
                          "currency": difference.get("currency"), "as_of": difference.get("as_of")},
                "source": {"kind": account["source_kind"], "ref": account["source_ref"],
                           "observed_on": account["observed_on"]},
                "valid_from": difference.get("as_of") or account["valid_from"],
                "institution": difference.get("institution"),
            }
            # A statement is the source of truth for the balance it covers: the person's estimate is closed
            # when the statement begins and kept in history, with nothing to ask.
            start = max(str(proposed["valid_from"]), str(stated_row["valid_from"]))
            self._close(stated_row["id"], start)
            revision = self._db.execute("SELECT revision FROM clients WHERE id = ?", (client_id,)).fetchone()[0]
            self._insert_fact(
                client_id,
                {"key": stated_key, "value": None, "source": proposed["source"], "confidence": "reported",
                 "expires_on": None, "valid_from": start},
                revision, now, status="replaced",
            )
            opened.append({"key": stated_key, "replaced_by": matching[0], "stated": stated_value,
                           "statement": value, "as_of": difference.get("as_of")})
        return opened

    def contradictions(self, client_id: str, status: str | None = "pending") -> list[dict[str, Any]]:
        """Contradictions waiting for the person (or all of them with ``status=None``)."""

        client_id = _required_text(client_id, "client_id")
        with self._lock:
            with self._read_transaction():
                self._client_row(client_id)
                sql = "SELECT * FROM contradictions WHERE client_id = ?"
                params: tuple[Any, ...] = (client_id,)
                if status is not None:
                    sql += " AND status = ?"
                    params += (status,)
                return [
                    self._contradiction_from_row(row)
                    for row in self._db.execute(sql + " ORDER BY created_at, id", params)
                ]

    def resolve_contradiction(
        self, client_id: str, contradiction_id: str, choice: str, valid_from: str | None = None,
    ) -> dict[str, Any]:
        """Apply the person's answer to a pending contradiction.

        * ``keep``: their value stays; the same proposal is not asked again.
        * ``use_new``: theirs was wrong; it is marked ``corrected`` and the new
          value (or, for a statement under another key, the statement alone)
          takes its place over the same period.
        * ``changed``: both were true at different times; the old value is
          closed at ``valid_from`` (default: the new evidence's date) and the
          new one is valid from then.
        """

        client_id = _required_text(client_id, "client_id")
        contradiction_id = _required_text(contradiction_id, "contradiction_id")
        if choice not in _CONTRADICTION_CHOICES:
            raise ValidationError(f"choice must be one of {', '.join(_CONTRADICTION_CHOICES)}")
        if valid_from is not None:
            if choice != "changed":
                raise ValidationError("valid_from applies only to choice=changed")
            if _iso_date(valid_from, "valid_from") > _today() + timedelta(days=1):
                raise ValidationError("valid_from must not be in the future")
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                client = self._client_row(client_id)
                row = self._db.execute(
                    "SELECT * FROM contradictions WHERE id = ? AND client_id = ?",
                    (contradiction_id, client_id),
                ).fetchone()
                if row is None:
                    raise ContradictionNotFoundError(
                        f"contradiction {contradiction_id!r} does not exist for this client"
                    )
                if row["status"] != "pending":
                    raise ValidationError(f"this contradiction was already resolved ({row['status']})")
                current = self._current_rows(client_id, [row["key"]]).get(row["key"])
                if current is None or current["id"] != row["current_fact_id"]:
                    raise ValidationError(
                        f"{row['key']} changed after this question was raised; reload the contradictions"
                    )
                proposed = json.loads(row["proposed_json"])
                start = revision = None
                if choice != "keep":
                    if choice == "use_new":
                        start = current["valid_from"]
                    else:
                        start = valid_from or proposed.get("valid_from") or proposed["source"]["observed_on"]
                        if start < current["valid_from"]:
                            raise ValidationError(
                                f"valid_from must not precede {current['valid_from']}, "
                                "when the current value began"
                            )
                    revision = client["revision"] + 1
                    self._db.execute("UPDATE clients SET revision = ? WHERE id = ?", (revision, client_id))
                    self._close(current["id"], start, corrected=choice == "use_new")
                    if row["kind"] == "same_key":
                        self._insert_fact(
                            client_id, {**proposed, "key": row["key"], "valid_from": start}, revision, now
                        )
                    else:
                        self._insert_fact(
                            client_id,
                            {"key": row["key"], "value": None, "source": proposed["source"],
                             "confidence": "reported", "expires_on": None, "valid_from": start},
                            revision, now, status="replaced",
                        )
                status = {"keep": "kept", "use_new": "used_new", "changed": "changed"}[choice]
                self._db.execute(
                    "UPDATE contradictions SET status = ?, resolved_at = ?, resolution_json = ? "
                    "WHERE id = ?",
                    (status, now, _json({"choice": choice, "valid_from": start, "revision": revision}),
                     contradiction_id),
                )
                result = self._receipt(client_id, revision, None, False, [], [])
                del result["needs_user"]
                result["contradiction"] = self._contradiction_from_row(self._db.execute(
                    "SELECT * FROM contradictions WHERE id = ?", (contradiction_id,)
                ).fetchone())
                self._commit()
            except Exception:
                self._rollback()
                raise
        return result

    def history(self, client_id: str, key: str) -> list[dict[str, Any]]:
        client_id = _required_text(client_id, "client_id")
        key = _required_text(key, "key")
        with self._lock:
            self._ensure_open()
            self._client_row(client_id)
            rows = self._db.execute(
                "SELECT * FROM facts WHERE client_id = ? AND key = ? "
                "ORDER BY revision, recorded_at, id",
                (client_id, key),
            ).fetchall()
            return [self._fact_from_row(row) for row in rows]

    def timeline(self, client_id: str, key: str) -> dict[str, Any]:
        """One key's valid-time history, newest first, with a readable line.

        e.g. "MXN 85,000 since 2026-03; MXN 78,000 from 2025-01 to 2026-03".
        """

        revisions = self.history(client_id, key)
        ordered = sorted(revisions, key=lambda f: (f["valid_from"] or "", f["revision"]), reverse=True)
        entries = [{**fact, "text": _period_text(fact)} for fact in ordered]
        return {"key": key, "text": "; ".join(e["text"] for e in entries), "entries": entries}

    def _eligible_evidence(self, client_id: str, evidence_ids: Any) -> list[str]:
        if not isinstance(evidence_ids, list):
            raise ValidationError("evidence_ids must be a nonempty list of fact ids")
        ids = [_required_text(value, "evidence_id") for value in evidence_ids]
        if not ids:
            raise ValidationError("evidence_ids must be a nonempty list of fact ids")
        if len(ids) != len(set(ids)):
            raise ValidationError("evidence_ids cannot contain duplicates")
        problems = self._evidence_problems(client_id, ids)
        if problems:
            raise IneligibleEvidenceError("ineligible evidence: " + "; ".join(problems))
        return ids

    def save_decision(
        self,
        client_id: str,
        title: str,
        rationale: str,
        expected_revision: int,
        evidence_ids: Sequence[str],
        alternatives: Any = None,
    ) -> dict[str, Any]:
        client_id = _required_text(client_id, "client_id")
        title = _required_text(title, "title")
        rationale = _required_text(rationale, "rationale")
        expected_revision = _revision(expected_revision)
        if alternatives is None:
            alternatives = []
        if not isinstance(alternatives, list):
            raise ValidationError("alternatives must be a JSON list")
        alternatives_json = _json(alternatives)
        now = _utc_now()
        decision_id = uuid.uuid4().hex
        with self._lock:
            self._begin()
            try:
                self._check_revision(client_id, expected_revision)
                ids = self._eligible_evidence(client_id, evidence_ids)
                self._db.execute(
                    "INSERT INTO decisions(id, client_id, title, rationale, input_revision, "
                    "evidence_ids_json, alternatives_json, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
                    (
                        decision_id,
                        client_id,
                        title,
                        rationale,
                        expected_revision,
                        _json(ids),
                        alternatives_json,
                        now,
                        now,
                    ),
                )
                self._db.execute(
                    "INSERT INTO decision_events(decision_id, status, recorded_at) "
                    "VALUES (?, 'proposed', ?)",
                    (decision_id, now),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise
            return self._decision(client_id, decision_id)

    def _decision(self, client_id: str, decision_id: str) -> dict[str, Any]:
        with self._read_transaction():
            row = self._db.execute(
                "SELECT * FROM decisions WHERE id = ? AND client_id = ?",
                (decision_id, client_id),
            ).fetchone()
            return self._decision_from_row(row)

    def set_decision_status(
        self,
        client_id: str,
        decision_id: str,
        status: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Record a person's choice; only changed or expired cited evidence blocks it."""

        client_id = _required_text(client_id, "client_id")
        decision_id = _required_text(decision_id, "decision_id")
        if not isinstance(status, str) or status not in _DECISION_STATUSES:
            raise ValidationError("status must be 'accepted' or 'dismissed'")
        if expected_revision is not None:
            expected_revision = _revision(expected_revision)
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                self._check_revision(client_id, expected_revision)
                row = self._db.execute(
                    "SELECT * FROM decisions WHERE id = ? AND client_id = ?",
                    (decision_id, client_id),
                ).fetchone()
                if row is None:
                    raise DecisionNotFoundError(
                        f"decision {decision_id!r} does not exist for this client"
                    )
                if row["status"] not in {"proposed", status}:
                    raise ValidationError(
                        "a resolved decision cannot change status; create a new proposal"
                    )
                if status == "accepted":
                    problems = self._evidence_problems(
                        client_id, json.loads(row["evidence_ids_json"])
                    )
                    if problems:
                        raise IneligibleEvidenceError(
                            "cannot accept; cited evidence no longer supports it: "
                            + "; ".join(problems)
                            + ". Refresh the evidence and create a new proposal."
                        )
                if row["status"] == "proposed":
                    self._db.execute(
                        "UPDATE decisions SET status = ?, updated_at = ? "
                        "WHERE id = ? AND client_id = ? AND status = 'proposed'",
                        (status, now, decision_id, client_id),
                    )
                    self._db.execute(
                        "INSERT INTO decision_events(decision_id, status, recorded_at) "
                        "VALUES (?, ?, ?)",
                        (decision_id, status, now),
                    )
                self._commit()
            except Exception:
                self._rollback()
                raise
            return self._decision(client_id, decision_id)

    def export_client(self, client_id: str) -> dict[str, Any]:
        client_id = _required_text(client_id, "client_id")
        with self._lock:
            with self._read_transaction():
                client = self._client_row(client_id)
                facts = self._db.execute(
                    "SELECT * FROM facts WHERE client_id = ? "
                    "ORDER BY revision, key, recorded_at, id",
                    (client_id,),
                ).fetchall()
                decisions = self._db.execute(
                    "SELECT * FROM decisions WHERE client_id = ? ORDER BY created_at, id",
                    (client_id,),
                ).fetchall()
                decision_exports = []
                latest = self._latest_ids(client_id) if decisions else {}
                for row in decisions:
                    decision = self._decision_from_row(row, latest)
                    decision["events"] = self._decision_events(row["id"])
                    decision_exports.append(decision)
                return {
                    "schema_version": SCHEMA_VERSION,
                    "client": {
                        "id": client["id"],
                        "display_name": client["display_name"],
                        "revision": client["revision"],
                        "created_at": client["created_at"],
                    },
                    "facts": [self._fact_from_row(row) for row in facts],
                    "decisions": decision_exports,
                    "contradictions": [
                        self._contradiction_from_row(row) for row in self._db.execute(
                            "SELECT * FROM contradictions WHERE client_id = ? ORDER BY created_at, id",
                            (client_id,),
                        )
                    ],
                    "auxiliary": {
                        row["namespace"]: _exportable(row["namespace"], json.loads(row["value_json"]))
                        for row in self._db.execute(
                            "SELECT namespace, value_json FROM auxiliary WHERE client_id = ?", (client_id,)
                        )
                    },
                    "ledger": self._ledger_rows(client_id, include_batches=True),
                    "orders": self._order_rows(client_id),
                    "conversations": self._conversation_rows(client_id),
                }

    # ---- market-data cache (shared, not client data; see wealth/prices.py)

    def market_rows(self, symbol: str, kind: str, start: str | None = None,
                    end: str | None = None) -> list[dict[str, Any]]:
        """Cached daily values of one provider symbol, oldest first, within [start, end]."""
        sql = "SELECT * FROM market_prices WHERE symbol = ? AND kind = ?"
        args: list[Any] = [symbol, kind]
        if start:
            sql += " AND date >= ?"
            args.append(start)
        if end:
            sql += " AND date <= ?"
            args.append(end)
        with self._lock:
            self._ensure_open()
            return [dict(row) for row in self._db.execute(sql + " ORDER BY date", args)]

    def market_fetches(self, symbol: str, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent fetch attempts for a symbol, newest first."""
        with self._lock:
            self._ensure_open()
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM market_fetches WHERE symbol = ? AND kind = ? ORDER BY retrieved_at DESC LIMIT ?",
                (symbol, kind, int(limit)))]

    def put_market(self, rows: Sequence[Mapping[str, Any]], fetch: Mapping[str, Any]) -> int:
        """Store fetched rows and log the fetch in one transaction.

        A row for a past day that was already fetched after that day is final and
        is not overwritten; a row fetched on its own day (an intraday value) is.
        """
        with self._lock:
            self._begin()
            try:
                written = 0
                for row in rows:
                    cursor = self._db.execute(
                        "INSERT INTO market_prices(symbol, kind, date, value, currency, source, retrieved_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol, kind, date) DO UPDATE SET "
                        "value = excluded.value, currency = excluded.currency, source = excluded.source, "
                        "retrieved_at = excluded.retrieved_at "
                        "WHERE substr(market_prices.retrieved_at, 1, 10) <= market_prices.date",
                        (row["symbol"], row["kind"], row["date"], str(row["value"]), row.get("currency"),
                         row["source"], row["retrieved_at"]))
                    written += cursor.rowcount
                self._db.execute(
                    "INSERT INTO market_fetches(symbol, kind, start, end, retrieved_at, status, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (fetch["symbol"], fetch["kind"], fetch["start"], fetch["end"], fetch["retrieved_at"],
                     fetch.get("status", "ok"), fetch.get("detail")))
                self._commit()
                return written
            except Exception:
                self._rollback()
                raise

    def market_summary(self) -> list[dict[str, Any]]:
        """One line per cached symbol and kind: rows, first and last date, last retrieval."""
        with self._lock:
            self._ensure_open()
            return [dict(row) for row in self._db.execute(
                "SELECT symbol, kind, COUNT(*) AS rows, MIN(date) AS first, MAX(date) AS last, "
                "MAX(retrieved_at) AS retrieved_at, MAX(currency) AS currency "
                "FROM market_prices GROUP BY symbol, kind ORDER BY symbol, kind")]

    def auxiliary(self, client_id: str, namespace: str) -> dict:
        """Read derived state without taking a write lock or creating a row."""
        client_id = _required_text(client_id, "client_id")
        namespace = _required_text(namespace, "namespace")
        if namespace not in _AUXILIARY:
            raise ValidationError("unknown auxiliary namespace")
        with self._lock:
            with self._read_transaction():
                self._client_row(client_id)
                row = self._db.execute(
                    "SELECT value_json FROM auxiliary "
                    "WHERE client_id = ? AND namespace = ?",
                    (client_id, namespace),
                ).fetchone()
                return json.loads(row["value_json"]) if row else {}

    def update_auxiliary(self, client_id: str, namespace: str, update) -> dict:
        """Serialize derived-state read/modify/write across processes."""
        client_id = _required_text(client_id, "client_id")
        namespace = _required_text(namespace, "namespace")
        if namespace not in _AUXILIARY:
            raise ValidationError("unknown auxiliary namespace")
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                row = self._db.execute(
                    "SELECT value_json FROM auxiliary WHERE client_id = ? AND namespace = ?",
                    (client_id, namespace),
                ).fetchone()
                result = update(json.loads(row["value_json"]) if row else {})
                if not isinstance(result, dict):
                    raise ValidationError("auxiliary value must be an object")
                self._db.execute(
                    "INSERT INTO auxiliary(client_id, namespace, value_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(client_id, namespace) DO UPDATE SET value_json = excluded.value_json",
                    (client_id, namespace, _json(result)),
                )
                self._commit()
                return result
            except Exception:
                self._rollback()
                raise

    # -- order audit trail (wealth/execution) -------------------------------

    def record_order_event(self, client_id: str, event: Mapping[str, Any]) -> int:
        """Append one redacted audit row; rows are never updated or deleted."""
        client_id = _required_text(client_id, "client_id")
        if not isinstance(event, Mapping):
            raise ValidationError("an order event must be an object")
        kind = event.get("event")
        if kind not in _ORDER_EVENTS:
            raise ValidationError("unknown order event")
        if event.get("mode") not in ("paper", "live"):
            raise ValidationError("order events need mode paper or live")
        payload = event.get("payload") or {}
        _validate_json(payload, "payload")
        line = event.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            raise ValidationError("line must be an integer")
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                cursor = self._db.execute(
                    "INSERT INTO orders(client_id, ticket_id, event, broker, mode, line, client_order_id, "
                    "broker_order_id, payload_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (client_id, _required_text(event.get("ticket_id"), "ticket_id"), kind,
                     _required_text(event.get("broker"), "broker"), event["mode"], line,
                     event.get("client_order_id"), event.get("broker_order_id"), _json(payload), _utc_now()),
                )
                self._db.execute("COMMIT")
                return int(cursor.lastrowid)
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _order_rows(self, client_id: str, ticket_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM orders WHERE client_id = ?" + (" AND ticket_id = ?" if ticket_id else "") + " ORDER BY seq"
        rows = self._db.execute(sql, (client_id, ticket_id) if ticket_id else (client_id,)).fetchall()
        return [{"seq": row["seq"], "ticket_id": row["ticket_id"], "event": row["event"], "broker": row["broker"],
                 "mode": row["mode"], "line": row["line"], "client_order_id": row["client_order_id"],
                 "broker_order_id": row["broker_order_id"], "payload": json.loads(row["payload_json"]),
                 "recorded_at": row["recorded_at"]} for row in rows]

    def order_events(self, client_id: str, ticket_id: str | None = None) -> list[dict[str, Any]]:
        client_id = _required_text(client_id, "client_id")
        with self._lock:
            with self._read_transaction():
                self._client_row(client_id)
                return self._order_rows(client_id, ticket_id)

    # -- chat conversation (wealth/web.py) ------------------------------------

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> dict[str, Any]:
        message: dict[str, Any] = {"id": row["message_id"], "role": row["role"], "content": row["content"]}
        for field, column in (("attachments", "attachments_json"), ("memory", "memory_json"),
                              ("views", "views_json")):
            value = json.loads(row[column]) if row[column] else None
            if value:
                message[field] = value
        if "status" in row.keys() and row["status"] in MESSAGE_STATUSES:
            message["status"] = row["status"]
        return message

    def _conversation_rows(self, client_id: str) -> list[dict[str, Any]]:
        result = []
        for conv in self._db.execute(
            "SELECT * FROM conversations WHERE client_id = ? ORDER BY seq", (client_id,)
        ).fetchall():
            messages = [
                {**self._message_from_row(row), "created_at": row["created_at"]}
                for row in self._db.execute(
                    "SELECT * FROM conversation_messages WHERE client_id = ? AND conversation_id = ? ORDER BY seq",
                    (client_id, conv["id"]),
                )
            ]
            result.append({"id": conv["id"], "thread_id": conv["thread_id"], "created_at": conv["created_at"],
                           "updated_at": conv["updated_at"], "messages": messages})
        return result

    def conversation(self, client_id: str, limit: int = CONVERSATION_LIMIT) -> dict[str, Any]:
        """The current (newest) conversation: its id, Codex thread and last ``limit`` messages, oldest first.

        With no conversation yet, ``id`` and ``thread_id`` are None and there are no messages.
        """
        client_id = _required_text(client_id, "client_id")
        with self._lock:
            with self._read_transaction():
                self._client_row(client_id)
                conv = self._db.execute(
                    "SELECT * FROM conversations WHERE client_id = ? ORDER BY seq DESC LIMIT 1", (client_id,)
                ).fetchone()
                if conv is None:
                    return {"id": None, "thread_id": None, "messages": []}
                rows = self._db.execute(
                    "SELECT * FROM conversation_messages WHERE client_id = ? AND conversation_id = ? "
                    "ORDER BY seq DESC LIMIT ?", (client_id, conv["id"], max(0, int(limit))),
                ).fetchall()
                return {"id": conv["id"], "thread_id": conv["thread_id"],
                        "messages": [self._message_from_row(row) for row in reversed(rows)]}

    def start_conversation(self, client_id: str) -> str:
        """Start a new current conversation; earlier ones are kept."""
        client_id = _required_text(client_id, "client_id")
        conversation_id = uuid.uuid4().hex
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                self._db.execute(
                    "INSERT INTO conversations(client_id, id, thread_id, created_at, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?)", (client_id, conversation_id, now, now))
                self._commit()
            except Exception:
                self._rollback()
                raise
        return conversation_id

    def append_messages(self, client_id: str, conversation_id: str, messages: Sequence[Mapping[str, Any]],
                        thread_id: str | None = None) -> None:
        """Append messages, redacted, to a conversation and record its Codex thread, in one write.

        ``thread_id`` None leaves the stored thread as it is; an empty string clears it.
        """
        from .ingest.redact import redact_text

        client_id = _required_text(client_id, "client_id")
        conversation_id = _required_text(conversation_id, "conversation_id")
        rows = []
        for message in messages:
            if not isinstance(message, Mapping) or message.get("role") not in ("user", "assistant"):
                raise ValidationError("a conversation message needs role user or assistant")
            attachments = [
                {**item, "name": redact_text(str(item["name"]))} if isinstance(item, Mapping) and "name" in item
                else item for item in (message.get("attachments") or [])
            ]
            extras = (attachments, list(message.get("memory") or []), list(message.get("views") or []))
            for extra in extras:
                _validate_json(extra, "message")
            status = message.get("status")
            if status is not None and status not in MESSAGE_STATUSES:
                raise ValidationError("a conversation message status must be stopped or failed")
            rows.append((_required_text(message.get("id"), "message id"), message["role"],
                         redact_text(str(message.get("content") or "")),
                         *(_json(extra) if extra else None for extra in extras), status))
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                self._db.execute(
                    "INSERT OR IGNORE INTO conversations(client_id, id, thread_id, created_at, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?)", (client_id, conversation_id, now, now))
                for row in rows:
                    self._db.execute(
                        "INSERT INTO conversation_messages(client_id, conversation_id, message_id, role, content, "
                        "attachments_json, memory_json, views_json, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (client_id, conversation_id, *row, now))
                if thread_id is not None:
                    self._db.execute(
                        "UPDATE conversations SET thread_id = ?, updated_at = ? WHERE client_id = ? AND id = ?",
                        (thread_id or None, now, client_id, conversation_id))
                elif rows:
                    self._db.execute("UPDATE conversations SET updated_at = ? WHERE client_id = ? AND id = ?",
                                     (now, client_id, conversation_id))
                self._commit()
            except Exception:
                self._rollback()
                raise

    def set_message_memory(self, client_id: str, message_id: str, memory: Sequence[Mapping[str, Any]]) -> None:
        """Attach the memory receipts that arrived after a reply was saved (the one column that changes)."""
        client_id = _required_text(client_id, "client_id")
        message_id = _required_text(message_id, "message_id")
        items = list(memory or [])
        _validate_json(items, "memory")
        with self._lock:
            self._begin()
            try:
                self._db.execute(
                    "UPDATE conversation_messages SET memory_json = ? WHERE client_id = ? AND message_id = ?",
                    (_json(items) if items else None, client_id, message_id))
                self._commit()
            except Exception:
                self._rollback()
                raise

    # -- transaction ledger ------------------------------------------------

    def _ledger_rows(self, client_id: str, include_batches: bool = False) -> dict[str, Any]:
        def rows(sql: str) -> list[sqlite3.Row]:
            return self._db.execute(sql, (client_id,)).fetchall()

        entries = []
        for row in rows("SELECT seq, batch_id, data_json, posted_at FROM ledger_entries "
                        "WHERE client_id = ? ORDER BY seq"):
            entry = json.loads(row["data_json"])
            entry.update(seq=row["seq"], batch_id=row["batch_id"], posted_at=row["posted_at"])
            entries.append(entry)
        result = {
            "accounts": [json.loads(r["data_json"]) for r in rows(
                "SELECT data_json FROM ledger_accounts WHERE client_id = ? ORDER BY id")],
            "instruments": [json.loads(r["data_json"]) for r in rows(
                "SELECT data_json FROM ledger_instruments WHERE client_id = ? ORDER BY id")],
            "entries": entries,
            "fx": [{"date": r["date"], "base": r["base"], "quote": r["quote"], "rate": r["rate"],
                    "source": r["source"]} for r in rows(
                "SELECT * FROM ledger_fx WHERE client_id = ? ORDER BY date, base, quote")],
            "assertions": [json.loads(r["data_json"]) for r in rows(
                "SELECT data_json FROM ledger_assertions WHERE client_id = ? ORDER BY id")],
            "category_rules": [json.loads(r["data_json"]) for r in rows(
                "SELECT data_json FROM ledger_rules WHERE client_id = ? ORDER BY created_at, id")],
            "labels": [{"entry_id": r["entry_id"], "category": r["category"], "status": r["status"],
                        "source": r["source"], "recorded_at": r["recorded_at"]} for r in rows(
                "SELECT * FROM ledger_labels WHERE client_id = ? ORDER BY id")],
            "revision": entries[-1]["seq"] if entries else 0,
        }
        if include_batches:
            result["batches"] = [
                {"batch_id": r["batch_id"], "created_at": r["created_at"],
                 "receipt": json.loads(r["receipt_json"])}
                for r in rows("SELECT * FROM ledger_batches WHERE client_id = ? ORDER BY created_at, batch_id")
            ]
        return result

    def ledger(self, client_id: str) -> dict[str, Any]:
        """Return the client's whole ledger as plain JSON-safe data (one snapshot)."""

        client_id = _required_text(client_id, "client_id")
        with self._lock:
            with self._read_transaction():
                self._client_row(client_id)
                return self._ledger_rows(client_id)

    def _possible_duplicates(self, client_id: str, entry: Mapping[str, Any], identity: str) -> list[str]:
        from difflib import SequenceMatcher
        from .ledger.model import fold

        day = date.fromisoformat(entry["date"])
        low = (day - timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
        high = (day + timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
        candidates = self._db.execute(
            "SELECT id, data_json FROM ledger_entries WHERE client_id = ? AND account_id = ? "
            "AND kind = ? AND date BETWEEN ? AND ? AND amount IS ? AND currency IS ? "
            "AND instrument_id IS ? AND quantity IS ? AND source_identity != ? ORDER BY seq",
            (client_id, entry["account_id"], entry["kind"], low, high, entry.get("amount"),
             entry.get("currency"), entry.get("instrument_id"), entry.get("quantity"), identity),
        ).fetchall()
        mine = fold(entry.get("description"))
        matches = []
        for row in candidates:
            theirs = fold(json.loads(row["data_json"]).get("description"))
            if not mine or not theirs or SequenceMatcher(None, mine, theirs).ratio() >= DUPLICATE_SIMILARITY:
                matches.append(row["id"])
        return matches

    def post_ledger(self, client_id: str, batch: Mapping[str, Any]) -> dict[str, Any]:
        """Post a batch atomically and idempotently; return a receipt.

        Exact duplicates (same external id, or the same line content already
        posted from any source) are skipped.  Lines that closely resemble an
        entry from a *different* source are held for review, not posted; post
        them again with ``confirm_not_duplicate: true`` once the person agrees.
        Entries are append-only: correct one with a ``reversal`` plus a new line.
        """

        from .ledger.model import LedgerInputError, digest, normalize_batch, source_identity

        client_id = _required_text(client_id, "client_id")
        try:
            normalized, warnings = normalize_batch(batch)
        except LedgerInputError as exc:
            raise ValidationError(str(exc)) from exc
        payload_hash = digest(normalized)
        batch_id = normalized["batch_id"]
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                prior = self._db.execute(
                    "SELECT payload_hash, receipt_json FROM ledger_batches WHERE client_id = ? AND batch_id = ?",
                    (client_id, batch_id),
                ).fetchone()
                if prior is not None:
                    if prior["payload_hash"] != payload_hash:
                        raise RequestConflictError(
                            f"batch_id {batch_id!r} was already posted with different content; "
                            "use a new batch_id for new or corrected lines"
                        )
                    receipt = json.loads(prior["receipt_json"])
                    receipt["replayed"] = True
                    self._commit()
                    return receipt
                receipt: dict[str, Any] = {
                    "batch_id": batch_id, "replayed": False,
                    "accounts": {"created": [], "updated": [], "unchanged": []},
                    "instruments": {"created": [], "updated": [], "unchanged": []},
                    "posted": [], "duplicates": [], "held": [],
                    "fx": {"added": 0, "existing": 0}, "assertions": {"added": [], "existing": []},
                    "warnings": warnings,
                }
                for table, rows, label in (
                    ("ledger_accounts", normalized["accounts"], "accounts"),
                    ("ledger_instruments", normalized["instruments"], "instruments"),
                ):
                    for row in rows:
                        existing = self._db.execute(
                            f"SELECT data_json FROM {table} WHERE client_id = ? AND id = ?",
                            (client_id, row["id"]),
                        ).fetchone()
                        if existing is None:
                            receipt[label]["created"].append(row["id"])
                        elif json.loads(existing["data_json"]) == row:
                            receipt[label]["unchanged"].append(row["id"])
                            continue
                        else:
                            before = json.loads(existing["data_json"])
                            changed = sorted(k for k in {*before, *row} if before.get(k) != row.get(k))
                            if "currency" in changed:
                                raise ValidationError(
                                    f"{label[:-1]} {row['id']} currency cannot change once posted; "
                                    "create a new id"
                                )
                            receipt[label]["updated"].append({"id": row["id"], "fields": changed})
                        self._db.execute(
                            f"INSERT INTO {table}(client_id, id, data_json, updated_at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(client_id, id) DO UPDATE SET data_json = excluded.data_json, "
                            "updated_at = excluded.updated_at",
                            (client_id, row["id"], _json(row), now),
                        )
                for rate in normalized["fx"]:
                    existing = self._db.execute(
                        "SELECT rate FROM ledger_fx WHERE client_id = ? AND date = ? AND base = ? AND quote = ?",
                        (client_id, rate["date"], rate["base"], rate["quote"]),
                    ).fetchone()
                    if existing is None:
                        self._db.execute(
                            "INSERT INTO ledger_fx(client_id, date, base, quote, rate, source) VALUES (?, ?, ?, ?, ?, ?)",
                            (client_id, rate["date"], rate["base"], rate["quote"], rate["rate"], rate["source"]),
                        )
                        receipt["fx"]["added"] += 1
                    elif existing["rate"] == rate["rate"]:
                        receipt["fx"]["existing"] += 1
                    else:
                        raise ValidationError(
                            f"fx {rate['base']}/{rate['quote']} on {rate['date']} is already {existing['rate']}; "
                            f"refusing conflicting rate {rate['rate']}"
                        )
                accounts = {r["id"] for r in self._db.execute(
                    "SELECT id FROM ledger_accounts WHERE client_id = ?", (client_id,))}
                instruments = {r["id"]: json.loads(r["data_json"]) for r in self._db.execute(
                    "SELECT id, data_json FROM ledger_instruments WHERE client_id = ?", (client_id,))}
                problems = []
                for entry in normalized["transactions"]:
                    line = f"transactions[{entry['line']}]"
                    venue = instruments.get(entry.get("instrument_id"), {}).get("venue")
                    carries_basis = entry["kind"] in {"buy", "sell"} or entry.get("cost_basis") is not None
                    if venue == "sic" and carries_basis and entry.get("currency") != "MXN":
                        problems.append(f"{line}: {entry['instrument_id']} is a SIC listing; its trades and basis "
                                        "must be in MXN (post the MXN amount the statement shows)")
                    if entry["account_id"] not in accounts:
                        problems.append(f"{line}.account_id {entry['account_id']!r} is not a ledger account")
                    for name in ("instrument_id", "new_instrument_id"):
                        if entry.get(name) and entry[name] not in instruments:
                            problems.append(f"{line}.{name} {entry[name]!r} is not a ledger instrument")
                    if entry.get("counterparty_account_id") and entry["counterparty_account_id"] not in accounts:
                        problems.append(f"{line}.counterparty_account_id is not a ledger account")
                for assertion in normalized["balance_assertions"]:
                    if assertion["account_id"] not in accounts:
                        problems.append(f"balance assertion {assertion['id']} names an unknown account")
                    if assertion.get("instrument_id") and assertion["instrument_id"] not in instruments:
                        problems.append(f"balance assertion {assertion['id']} names an unknown instrument")
                if problems:
                    raise ValidationError("; ".join(problems))
                seq = self._db.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM ledger_entries WHERE client_id = ?", (client_id,)
                ).fetchone()[0]
                reversed_in_batch: set[str] = set()
                for entry in normalized["transactions"]:
                    line = entry.pop("line")
                    identity = source_identity(entry["source"])
                    existing = self._db.execute(
                        "SELECT id FROM ledger_entries WHERE client_id = ? AND dedupe_hash = ?",
                        (client_id, entry["dedupe_hash"]),
                    ).fetchone()
                    if existing is not None:
                        receipt["duplicates"].append({"line": line, "id": existing["id"], "reason": "already posted"})
                        continue
                    if entry["kind"] == "reversal":
                        target = self._db.execute(
                            "SELECT kind, account_id FROM ledger_entries WHERE client_id = ? AND id = ?",
                            (client_id, entry["reverses_id"]),
                        ).fetchone()
                        if target is None:
                            raise ValidationError(f"transactions[{line}] reverses unknown entry {entry['reverses_id']}")
                        if target["kind"] == "reversal":
                            raise ValidationError(f"transactions[{line}] cannot reverse a reversal; post the entry again")
                        if target["account_id"] != entry["account_id"]:
                            raise ValidationError(f"transactions[{line}] must use the reversed entry's account")
                        taken = self._db.execute(
                            "SELECT id FROM ledger_entries WHERE client_id = ? AND reverses_id = ?",
                            (client_id, entry["reverses_id"]),
                        ).fetchone()
                        if taken is not None or entry["reverses_id"] in reversed_in_batch:
                            raise ValidationError(f"entry {entry['reverses_id']} is already reversed")
                        reversed_in_batch.add(entry["reverses_id"])
                    elif not entry.get("confirm_not_duplicate"):
                        matches = self._possible_duplicates(client_id, entry, identity)
                        if matches:
                            receipt["held"].append({
                                "line": line, "id": entry["id"], "matches": matches,
                                "reason": "resembles an entry from another statement (same account, amount, "
                                          f"within {DUPLICATE_WINDOW_DAYS} days); repost with "
                                          "confirm_not_duplicate=true if it is a separate transaction",
                            })
                            continue
                    seq += 1
                    self._db.execute(
                        "INSERT INTO ledger_entries(client_id, id, seq, batch_id, account_id, kind, date, "
                        "amount, currency, instrument_id, quantity, dedupe_hash, source_identity, "
                        "reverses_id, data_json, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (client_id, entry["id"], seq, batch_id, entry["account_id"], entry["kind"], entry["date"],
                         entry.get("amount"), entry.get("currency"), entry.get("instrument_id"),
                         entry.get("quantity"), entry["dedupe_hash"], identity, entry.get("reverses_id"),
                         _json(entry), now),
                    )
                    receipt["posted"].append(entry["id"])
                    if entry.get("category"):
                        self._db.execute(
                            "INSERT INTO ledger_labels(client_id, entry_id, category, status, source, recorded_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (client_id, entry["id"], entry["category"],
                             "confirmed" if entry["confidence"] == "confirmed" else "reported",
                             "statement:" + identity, now),
                        )
                for assertion in normalized["balance_assertions"]:
                    added = self._db.execute(
                        "INSERT OR IGNORE INTO ledger_assertions(client_id, id, data_json) VALUES (?, ?, ?)",
                        (client_id, assertion["id"], _json(assertion)),
                    ).rowcount
                    receipt["assertions"]["added" if added else "existing"].append(assertion["id"])
                if receipt["held"]:
                    receipt["warnings"].append(
                        f"{len(receipt['held'])} line(s) held as possible duplicates of another statement; "
                        "nothing was guessed. Confirm each with the person."
                    )
                receipt["ledger_revision"] = seq
                self._db.execute(
                    "INSERT INTO ledger_batches(client_id, batch_id, payload_hash, receipt_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (client_id, batch_id, payload_hash, _json(receipt), now),
                )
                self._commit()
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ValidationError(f"ledger write rejected: {exc}") from exc
            except Exception:
                self._rollback()
                raise
        return receipt

    def add_category_rule(self, client_id: str, rule: Mapping[str, Any]) -> dict[str, Any]:
        """Remember a categorisation rule (append-only; identical content is idempotent)."""

        from .ledger.model import digest

        client_id = _required_text(client_id, "client_id")
        if not isinstance(rule, Mapping):
            raise ValidationError("rule must be an object")
        rule = dict(rule)
        _validate_json(rule, "rule")
        rule_id = rule.get("id") or "rule_" + digest({k: v for k, v in rule.items() if k != "id"})[:20]
        rule["id"] = rule_id
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                existing = self._db.execute(
                    "SELECT data_json FROM ledger_rules WHERE client_id = ? AND id = ?", (client_id, rule_id)
                ).fetchone()
                if existing is not None and json.loads(existing["data_json"]) != rule:
                    raise ValidationError(f"rule {rule_id} already exists with different content")
                if existing is None:
                    self._db.execute(
                        "INSERT INTO ledger_rules(client_id, id, data_json, created_at) VALUES (?, ?, ?, ?)",
                        (client_id, rule_id, _json(rule), _utc_now()),
                    )
                self._commit()
            except Exception:
                self._rollback()
                raise
        return rule

    def label_entry(self, client_id: str, entry_id: str, category: str, status: str, source: str) -> dict[str, Any]:
        """Append a category label for an entry; the latest label wins, history is kept."""

        client_id = _required_text(client_id, "client_id")
        entry_id = _required_text(entry_id, "entry_id")
        category = _required_text(category, "category").strip().lower()
        source = _required_text(source, "source")
        if status not in _CONFIDENCES:
            raise ValidationError(f"status must be one of {sorted(_CONFIDENCES)!r}")
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                if self._db.execute(
                    "SELECT 1 FROM ledger_entries WHERE client_id = ? AND id = ?", (client_id, entry_id)
                ).fetchone() is None:
                    raise ValidationError(f"entry {entry_id!r} is not in this client's ledger")
                self._db.execute(
                    "INSERT INTO ledger_labels(client_id, entry_id, category, status, source, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (client_id, entry_id, category, status, source, now),
                )
                self._commit()
            except Exception:
                self._rollback()
                raise
        return {"entry_id": entry_id, "category": category, "status": status, "source": source, "recorded_at": now}

    def delete_client(
        self, client_id: str, confirm_client_id: str
    ) -> dict[str, Any]:
        client_id = _required_text(client_id, "client_id")
        confirm_client_id = _required_text(confirm_client_id, "confirm_client_id")
        if client_id != confirm_client_id:
            raise ValidationError("confirm_client_id must exactly match client_id")
        with self._lock:
            self._begin()
            try:
                self._client_row(client_id)
                self._db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
                self._commit()
            except Exception:
                self._rollback()
                raise
        removed = 0
        if self.path != ":memory:":
            from .service import upload_dir

            removed = secure_delete_tree(upload_dir(client_id, self.path))
        return {"deleted": True, "client_id": client_id, "uploads_removed": removed}


__all__ = [
    "CONVERSATION_LIMIT",
    "DEFAULT_REVIEW_DAYS",
    "DUPLICATE_SIMILARITY",
    "DUPLICATE_WINDOW_DAYS",
    "REVIEW_DAYS",
    "SCHEMA_VERSION",
    "STATED_TOLERANCE",
    "ClientExistsError",
    "ClientNotFoundError",
    "ContradictionNotFoundError",
    "DecisionNotFoundError",
    "IneligibleEvidenceError",
    "RequestConflictError",
    "StaleRevisionError",
    "StoreError",
    "ValidationError",
    "WealthStore",
    "describe",
    "is_stale",
    "merge_patch",
    "review_days",
    "secure_delete",
    "secure_delete_tree",
]
