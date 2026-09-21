"""Local, client-scoped evidence and decision storage.

The store deliberately has no network or model integration.  Text in sources and
facts is persisted as data and is never interpreted by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
_CONFIDENCES = frozenset({"confirmed", "reported", "inferred"})
_SOURCE_KINDS = frozenset({"user", "document", "tool", "inference"})
_DECISION_STATUSES = frozenset({"accepted", "dismissed"})
_FINANCIAL_KEYS = frozenset(
    {"plan.resources", "goals", "portfolio.snapshot", "income.schedule", "household"}
)
_FINANCIAL_PREFIXES = ("account.", "lot.", "tax.", "planning.", "research.", "analysis.")


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


class RequestConflictError(StoreError):
    """An idempotency key was reused for a different request."""


class DecisionNotFoundError(StoreError):
    """The requested decision does not exist for this client."""


class IneligibleEvidenceError(StoreError):
    """Evidence cannot support the requested decision operation."""


# Descriptive compatibility aliases for callers that prefer category names.
RevisionConflictError = StaleRevisionError
IdempotencyConflictError = RequestConflictError


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


def _is_financial_key(key: str) -> bool:
    return key in _FINANCIAL_KEYS or key.startswith(_FINANCIAL_PREFIXES)


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
                if row["value"] != str(SCHEMA_VERSION):
                    raise StoreError(
                        f"unsupported schema version {row['value']}; "
                        f"expected {SCHEMA_VERSION}"
                    )
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
                    VALUES('schema_version', '1');
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
                COMMIT;
                """
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

    def _check_revision(self, client_id: str, expected_revision: int) -> sqlite3.Row:
        row = self._client_row(client_id)
        if row["revision"] != expected_revision:
            raise StaleRevisionError(
                f"stale revision for client {client_id!r}: expected "
                f"{expected_revision}, current {row['revision']}"
            )
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
        }

    def _decision_needs_review(
        self, row: sqlite3.Row, client_revision: int, as_of: date | None = None
    ) -> bool:
        if row["input_revision"] != client_revision:
            return True
        evidence_ids = json.loads(row["evidence_ids_json"])
        if not evidence_ids:
            return True
        placeholders = ",".join("?" for _ in evidence_ids)
        evidence = self._db.execute(
            f"SELECT id, key, confidence, expires_on, revision FROM facts "
            f"WHERE client_id = ? AND id IN ({placeholders})",
            (row["client_id"], *evidence_ids),
        ).fetchall()
        if len(evidence) != len(evidence_ids):
            return True
        latest = {
            fact["key"]: fact["id"]
            for fact in self._db.execute(
                "SELECT f.key, f.id FROM facts f JOIN "
                "(SELECT key, MAX(revision) revision FROM facts "
                " WHERE client_id = ? GROUP BY key) latest "
                "ON latest.key = f.key AND latest.revision = f.revision "
                "WHERE f.client_id = ?",
                (row["client_id"], row["client_id"]),
            )
        }
        current_day = as_of or _today()
        return any(
            fact["confidence"] == "inferred"
            or latest.get(fact["key"]) != fact["id"]
            or (
                fact["expires_on"] is not None
                and date.fromisoformat(fact["expires_on"]) < current_day
            )
            for fact in evidence
        )

    def _decision_from_row(
        self, row: sqlite3.Row, client_revision: int
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "rationale": row["rationale"],
            "revision": row["input_revision"],
            "evidence_ids": json.loads(row["evidence_ids_json"]),
            "alternatives": json.loads(row["alternatives_json"]),
            "status": row["status"],
            "needs_review": self._decision_needs_review(row, client_revision),
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
                facts = self._db.execute(
                    "SELECT f.* FROM facts f JOIN "
                    "(SELECT key, MAX(revision) revision FROM facts "
                    " WHERE client_id = ? GROUP BY key) latest "
                    "ON latest.key = f.key AND latest.revision = f.revision "
                    "WHERE f.client_id = ? ORDER BY f.key",
                    (client_id, client_id),
                ).fetchall()
                decisions = self._db.execute(
                    "SELECT * FROM decisions WHERE client_id = ? ORDER BY created_at, id",
                    (client_id,),
                ).fetchall()
                return {
                    "client": {
                        "id": client["id"],
                        "display_name": client["display_name"],
                        "revision": client["revision"],
                    },
                    "facts": [self._fact_from_row(row) for row in facts],
                    "decisions": [
                        self._decision_from_row(row, client["revision"])
                        for row in decisions
                    ],
                }

    def _normalize_fact(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValidationError("each fact must be an object")
        if any(not isinstance(field, str) for field in raw):
            raise ValidationError("fact field names must be strings")
        allowed = {"key", "value", "source", "confidence", "expires_on"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(f"unknown fact fields: {sorted(unknown)!r}")
        key = _required_text(raw.get("key"), "fact.key")
        if "value" not in raw:
            raise ValidationError("fact.value is required")
        value = raw["value"]
        _validate_json(value, "fact.value")
        source = raw.get("source")
        if not isinstance(source, Mapping):
            raise ValidationError("fact.source must be an object")
        if any(not isinstance(field, str) for field in source):
            raise ValidationError("source field names must be strings")
        if set(source) != {"kind", "ref", "observed_on"}:
            raise ValidationError("fact.source requires exactly kind, ref, and observed_on")
        kind = source.get("kind")
        if not isinstance(kind, str) or kind not in _SOURCE_KINDS:
            raise ValidationError(f"source.kind must be one of {sorted(_SOURCE_KINDS)!r}")
        ref = _required_text(source.get("ref"), "source.ref")
        observed = _iso_date(source.get("observed_on"), "source.observed_on")
        if observed > _today():
            raise ValidationError("source.observed_on must not be in the future")
        confidence = raw.get("confidence", "reported")
        if not isinstance(confidence, str) or confidence not in _CONFIDENCES:
            raise ValidationError(f"confidence must be one of {sorted(_CONFIDENCES)!r}")
        if confidence == "confirmed" and kind != "user":
            raise ValidationError("only a user source can carry confirmed confidence")
        if kind == "inference" and confidence != "inferred":
            raise ValidationError("an inference source must carry inferred confidence")
        expires_text = raw.get("expires_on")
        expires = None
        if expires_text is not None:
            expires = _iso_date(expires_text, "fact.expires_on")
            if expires < observed:
                raise ValidationError("fact.expires_on must not precede source.observed_on")
        if _is_financial_key(key) and expires is None:
            raise ValidationError(f"financial fact {key!r} requires expires_on")
        return {
            "key": key,
            "value": value,
            "source": {"kind": kind, "ref": ref, "observed_on": observed.isoformat()},
            "confidence": confidence,
            "expires_on": expires.isoformat() if expires is not None else None,
        }

    def remember(
        self,
        client_id: str,
        facts: Sequence[Mapping[str, Any]],
        expected_revision: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        client_id = _required_text(client_id, "client_id")
        expected_revision = _revision(expected_revision)
        if not isinstance(facts, list) or not facts:
            raise ValidationError("facts must be a nonempty list")
        normalized = [self._normalize_fact(fact) for fact in facts]
        keys = [fact["key"] for fact in normalized]
        if len(keys) != len(set(keys)):
            raise ValidationError("a write cannot contain duplicate fact keys")
        if request_id is not None:
            request_id = _required_text(request_id, "request_id")
        payload = {
            "expected_revision": expected_revision,
            "facts": normalized,
        }
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
                                f"request_id {request_id!r} was reused with a different payload"
                            )
                        self._db.execute("COMMIT")
                        result = self.snapshot(client_id)
                        result["write_result"] = {
                            "request_id": request_id,
                            "resulting_revision": prior["resulting_revision"],
                            "replayed": True,
                        }
                        return result
                if client["revision"] != expected_revision:
                    raise StaleRevisionError(
                        f"stale revision for client {client_id!r}: expected "
                        f"{expected_revision}, current {client['revision']}"
                    )
                new_revision = expected_revision + 1
                changed = self._db.execute(
                    "UPDATE clients SET revision = ? WHERE id = ? AND revision = ?",
                    (new_revision, client_id, expected_revision),
                )
                if changed.rowcount != 1:
                    raise StaleRevisionError(f"stale revision for client {client_id!r}")
                for fact in normalized:
                    self._db.execute(
                        "INSERT INTO facts(id, client_id, key, value_json, source_kind, "
                        "source_ref, observed_on, confidence, expires_on, revision, recorded_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            uuid.uuid4().hex,
                            client_id,
                            fact["key"],
                            _json(fact["value"]),
                            fact["source"]["kind"],
                            fact["source"]["ref"],
                            fact["source"]["observed_on"],
                            fact["confidence"],
                            fact["expires_on"],
                            new_revision,
                            now,
                        ),
                    )
                if request_id is not None:
                    self._db.execute(
                        "INSERT INTO batches(client_id, request_id, payload_hash, "
                        "resulting_revision, created_at) VALUES (?, ?, ?, ?, ?)",
                        (client_id, request_id, payload_hash, new_revision, now),
                    )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        result = self.snapshot(client_id)
        result["write_result"] = {
            "request_id": request_id,
            "resulting_revision": new_revision,
            "replayed": False,
        }
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

    def _eligible_evidence(
        self, client_id: str, evidence_ids: Sequence[str]
    ) -> list[sqlite3.Row]:
        if not isinstance(evidence_ids, list):
            raise ValidationError("evidence_ids must be a nonempty list")
        ids = [_required_text(value, "evidence_id") for value in evidence_ids]
        if not ids:
            raise ValidationError("evidence_ids must be a nonempty list")
        if len(ids) != len(set(ids)):
            raise ValidationError("evidence_ids cannot contain duplicates")
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.execute(
            f"SELECT * FROM facts WHERE client_id = ? AND id IN ({placeholders})",
            (client_id, *ids),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        missing = [value for value in ids if value not in by_id]
        if missing:
            raise IneligibleEvidenceError(
                "evidence must belong to the active client: " + ", ".join(missing)
            )
        latest = {
            row["key"]: row["id"]
            for row in self._db.execute(
                "SELECT f.key, f.id FROM facts f JOIN "
                "(SELECT key, MAX(revision) revision FROM facts "
                " WHERE client_id = ? GROUP BY key) latest "
                "ON latest.key = f.key AND latest.revision = f.revision "
                "WHERE f.client_id = ?",
                (client_id, client_id),
            )
        }
        today = _today()
        for evidence_id in ids:
            row = by_id[evidence_id]
            if latest.get(row["key"]) != row["id"]:
                raise IneligibleEvidenceError(f"evidence {evidence_id!r} is not current")
            if row["confidence"] == "inferred":
                raise IneligibleEvidenceError(f"evidence {evidence_id!r} is inferred")
            if row["expires_on"] and date.fromisoformat(row["expires_on"]) < today:
                raise IneligibleEvidenceError(f"evidence {evidence_id!r} is expired")
        return [by_id[value] for value in ids]

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
                self._eligible_evidence(client_id, evidence_ids)
                ids = list(evidence_ids)
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
        return next(
            decision
            for decision in self.snapshot(client_id)["decisions"]
            if decision["id"] == decision_id
        )

    def set_decision_status(
        self,
        client_id: str,
        decision_id: str,
        status: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        client_id = _required_text(client_id, "client_id")
        decision_id = _required_text(decision_id, "decision_id")
        if not isinstance(status, str) or status not in _DECISION_STATUSES:
            raise ValidationError("status must be 'accepted' or 'dismissed'")
        expected_revision = _revision(expected_revision)
        now = _utc_now()
        with self._lock:
            self._begin()
            try:
                client = self._check_revision(client_id, expected_revision)
                row = self._db.execute(
                    "SELECT * FROM decisions WHERE id = ? AND client_id = ?",
                    (decision_id, client_id),
                ).fetchone()
                if row is None:
                    raise DecisionNotFoundError(
                        f"decision {decision_id!r} does not exist for this client"
                    )
                if row["status"] == status:
                    if status == "accepted":
                        if self._decision_needs_review(row, client["revision"]):
                            raise IneligibleEvidenceError(
                                "a stale decision or one with ineligible evidence "
                                "cannot be accepted"
                            )
                        self._eligible_evidence(
                            client_id, json.loads(row["evidence_ids_json"])
                        )
                    self._db.execute("COMMIT")
                elif row["status"] != "proposed":
                    raise ValidationError(
                        "a resolved decision cannot change status; create a new proposal"
                    )
                elif status == "accepted":
                    if self._decision_needs_review(row, client["revision"]):
                        raise IneligibleEvidenceError(
                            "a stale decision or one with ineligible evidence cannot be accepted"
                        )
                    self._eligible_evidence(
                        client_id, json.loads(row["evidence_ids_json"])
                    )
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
                else:
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
        return next(
            decision
            for decision in self.snapshot(client_id)["decisions"]
            if decision["id"] == decision_id
        )

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
                for row in decisions:
                    decision = self._decision_from_row(row, client["revision"])
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
                    "auxiliary": {
                        row["namespace"]: json.loads(row["value_json"])
                        for row in self._db.execute(
                            "SELECT namespace, value_json FROM auxiliary WHERE client_id = ?", (client_id,)
                        )
                    },
                }

    def auxiliary(self, client_id: str, namespace: str) -> dict:
        """Read derived state without taking a write lock or creating a row."""
        client_id = _required_text(client_id, "client_id")
        namespace = _required_text(namespace, "namespace")
        if namespace not in {"embeddings", "monitor"}:
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
        if namespace not in {"embeddings", "monitor"}:
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
    "SCHEMA_VERSION",
    "ClientExistsError",
    "ClientNotFoundError",
    "DecisionNotFoundError",
    "IdempotencyConflictError",
    "IneligibleEvidenceError",
    "RequestConflictError",
    "RevisionConflictError",
    "StaleRevisionError",
    "StoreError",
    "ValidationError",
    "WealthStore",
]
