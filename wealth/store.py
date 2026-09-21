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

from .situation.schema import SchemaError, validate as validate_canonical


SCHEMA_VERSION = 3
_CONFIDENCES = frozenset({"confirmed", "reported", "inferred"})
_SOURCE_KINDS = frozenset({"user", "document", "web", "tool", "inference", "connector", "pattern"})
# Sources that may not silently overwrite what the person told us (see ``remember``).
_CHALLENGER_KINDS = frozenset({"document", "web", "connector", "inference", "pattern"})
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


class IneligibleEvidenceError(StoreError):
    """Evidence cannot support the requested decision operation."""


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


def review_days(key: str) -> int:
    for pattern, days in REVIEW_DAYS:
        if key == pattern or (pattern.endswith(".") and key.startswith(pattern)):
            return days
    return DEFAULT_REVIEW_DAYS


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
    """RFC 7386 merge for objects; lists of objects with ``id`` merge by id."""

    if isinstance(patch, dict):
        merged = dict(current) if isinstance(current, dict) else {}
        for name, item in patch.items():
            if item is None:
                merged.pop(name, None)
            else:
                merged[name] = merge_patch(merged.get(name), item, f"{field}.{name}")
        return merged
    if isinstance(patch, list) and isinstance(current, list):
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
    "document": "a document ({ref})", "web": "a web page ({ref})", "connector": "a connected account",
    "inference": "my own reading", "pattern": "your transactions", "tool": "a calculation",
}


def _question(kind: str, key: str, current: Mapping[str, Any], proposed: Mapping[str, Any]) -> str:
    """The question to put to the person, in plain words (the agent translates it)."""

    since = current.get("valid_from")
    mine = f"You told me {key} is {describe(current['value'])}" + (f" (since {since})" if since else "")
    source = proposed["source"]
    where = _SOURCE_PHRASES.get(source["kind"], source["kind"]).format(ref=source["ref"])
    if kind == "stated_vs_statement":
        value = proposed["value"]
        institution = proposed.get("institution")
        return (f"{mine}{' at ' + institution if institution else ''}, but the statement "
                f"dated {value.get('as_of') or source['observed_on']} shows {describe(value)}. "
                "Keep your figure, use the statement, or did it change?")
    return (f"{mine}, but {where} says {describe(proposed['value'])} as of "
            f"{proposed.get('valid_from') or source['observed_on']}. "
            "Keep yours, use the new one, or did it change?")


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


class WealthStore:
    """A local SQLite evidence store.

    ``delete_client`` removes only this database's rows.  Copies previously
    exported by callers and external backups are outside that operation.
    """

    def __init__(self, path: str | os.PathLike[str]):
        if not isinstance(path, (str, os.PathLike)):
            raise ValidationError("path must be a filesystem path")
        self.path = os.fspath(path)
        if not self.path:
            raise ValidationError("path must not be empty")
        self._closed = False
        self._lock = threading.RLock()
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

    def _create_schema(self) -> None:
        with self._lock:
            existing_tables = {
                row["name"]
                for row in self._db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "metadata" in existing_tables:
                row = self._db.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if row is None:
                    raise StoreError("database metadata has no schema version")
                version = row["value"]
                base = _TABLES - {"contradictions"}
                if version == "1" and existing_tables >= base:
                    self._migrate_to_2()
                    version = "2"
                if version == "2" and existing_tables >= base:
                    self._migrate_to_3()
                    return
                if row["value"] != str(SCHEMA_VERSION):
                    raise StoreError(
                        f"unsupported schema version {row['value']}; "
                        f"expected {SCHEMA_VERSION}"
                    )
                if existing_tables >= _TABLES | _LEDGER_TABLES:
                    return
            elif existing_tables:
                raise StoreError("existing database has no wealth schema version")
            self._db.executescript(
                """
                BEGIN IMMEDIATE;
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
                """ + _LEDGER_SCHEMA + _CONTRADICTION_SCHEMA + """
                COMMIT;
                """
            )

    def _migrate_to_2(self) -> None:
        """Add ledger tables to a version-1 database in one transaction."""

        self._db.executescript(
            "BEGIN IMMEDIATE;"
            + _LEDGER_SCHEMA
            + "UPDATE metadata SET value = '2' WHERE key = 'schema_version'; COMMIT;"
        )

    def _migrate_to_3(self) -> None:
        """Add valid time and contradictions to a version-2 database in one transaction.

        Every existing revision becomes valid from its observation date and is
        closed where the next revision of the same key begins.  Old null
        retractions become ``forgotten`` tombstones.
        """

        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(facts)")}
        added = {"valid_from": "TEXT", "valid_to": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'"}
        self._db.executescript(
            "BEGIN IMMEDIATE;"
            + "".join(f"ALTER TABLE facts ADD COLUMN {name} {kind};"
                      for name, kind in added.items() if name not in columns)
            + """
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
            + "UPDATE metadata SET value = '3' WHERE key = 'schema_version'; COMMIT;"
        )

    def _begin(self) -> None:
        self._ensure_open()
        self._db.execute("BEGIN IMMEDIATE")

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
                        self._db.execute("COMMIT")
                        return result
                current_revision = client["revision"]
                if expected_revision is not None and current_revision != expected_revision:
                    raise StaleRevisionError(expected_revision, current_revision)
                current = {
                    key: row for key, row in self._current_rows(client_id, keys).items()
                    if row["status"] == "active"
                }
                needs_user: list[dict[str, Any]] = []
                to_write: list[tuple[dict[str, Any], sqlite3.Row | None]] = []
                for fact in normalized:
                    key = fact["key"]
                    prior_fact = current.get(key)
                    if fact["merge"] and prior_fact is not None and fact["value"] is not None:
                        fact["value"] = merge_patch(
                            json.loads(prior_fact["value_json"]), fact["value"], key
                        )
                    elif fact["merge"] and isinstance(fact["value"], dict):
                        fact["value"] = merge_patch({}, fact["value"])
                    if (
                        prior_fact is not None
                        and _protected(prior_fact)
                        and fact["source"]["kind"] in _CHALLENGER_KINDS
                        and _json(fact["value"]) != prior_fact["value_json"]
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
                    if prior_fact is not None and not fact["merge"] and expected_revision is None:
                        if not (prior_fact["confidence"] == "inferred"
                                and fact["confidence"] != "inferred"):
                            raise ValidationError(
                                f"{key} already has a value; send it with merge=true to "
                                f"update fields, or pass expected_revision={current_revision} to replace it"
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
                            raise ValidationError(f"{exc}; see fact_contract.schema") from exc
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
                    for opened in (self._statement_contradictions(client_id, statement_keys, request_id, now)
                                   if statement_keys else []):
                        needs_user.append(opened)
                        warnings.append(
                            f"the statement in {opened['proposed_key']} disagrees with what the person told us "
                            f"in {opened['key']}. Ask them (needs_user {opened['id']}); never pick a side silently"
                        )
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
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return result

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
        question = _question(kind, key, current_part, proposed)
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
        """Stated balances that a just-saved statement disagrees with (situation differences)."""

        from .situation.model import build, same_institution

        rows = {r["key"]: r for r in self._current_rows(client_id).values() if r["status"] == "active"}
        picture = build({"facts": [self._fact_from_row(r) for r in rows.values()]}, None, _today())
        institutions = {
            account["key"]: (account.get("institution") or "").strip().lower()
            for account in picture["accounts"] if account.get("key") in statement_keys
        }
        opened = []
        for difference in picture["differences"]:
            stated_key = difference.get("key") or ""
            stated_row = rows.get(stated_key)
            if (difference.get("kind") == "cash" or stated_row is None or not _protected(stated_row)
                    or not stated_key.startswith(("investment.", "cash."))):
                continue
            institution = (difference.get("institution") or "").strip().lower()
            matching = sorted(k for k, name in institutions.items()
                              if not institution or same_institution(name, institution))
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
            found = self._open_contradiction(
                client_id, "stated_vs_statement", stated_key, matching[0], stated_row, proposed,
                request_id, now,
            )
            if found is not None:
                opened.append(found)
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
                    raise ValidationError(
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
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
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
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
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
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
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
                        row["namespace"]: json.loads(row["value_json"])
                        for row in self._db.execute(
                            "SELECT namespace, value_json FROM auxiliary WHERE client_id = ?", (client_id,)
                        )
                    },
                    "ledger": self._ledger_rows(client_id, include_batches=True),
                }

    def auxiliary(self, client_id: str, namespace: str) -> dict:
        """Read derived state without taking a write lock or creating a row."""
        client_id = _required_text(client_id, "client_id")
        namespace = _required_text(namespace, "namespace")
        if namespace not in {"embeddings", "monitor", "ingest"}:
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
        if namespace not in {"embeddings", "monitor", "ingest"}:
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
                self._db.execute("COMMIT")
                return result
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
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
                    self._db.execute("COMMIT")
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
                self._db.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise ValidationError(f"ledger write rejected: {exc}") from exc
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
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
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
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
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
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
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return {"deleted": True, "client_id": client_id}


__all__ = [
    "DEFAULT_REVIEW_DAYS",
    "DUPLICATE_SIMILARITY",
    "DUPLICATE_WINDOW_DAYS",
    "REVIEW_DAYS",
    "SCHEMA_VERSION",
    "STATED_TOLERANCE",
    "ClientExistsError",
    "ClientNotFoundError",
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
]
