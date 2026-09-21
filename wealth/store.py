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


SCHEMA_VERSION = 1
_CONFIDENCES = frozenset({"confirmed", "reported", "inferred"})
_SOURCE_KINDS = frozenset({"user", "document", "web", "tool", "inference"})
_DECISION_STATUSES = frozenset({"accepted", "dismissed"})
_TABLES = frozenset(
    {"metadata", "clients", "facts", "batches", "decisions", "decision_events", "auxiliary"}
)
# Review horizons in days by key (exact) or prefix (ending in "."); the first match
# wins. A fact without an explicit expires_on is due for review this long after
# its observation. The horizon is a review deadline, not a prediction.
REVIEW_DAYS: tuple[tuple[str, int], ...] = (
    ("portfolio.snapshot", 30), ("household", 30), ("account.", 30), ("lot.", 30),
    ("analysis.", 30), ("plan.resources", 90), ("income.schedule", 90),
    ("planning.", 90), ("research.", 90), ("thesis.", 180),
)
DEFAULT_REVIEW_DAYS = 365  # client.profile, goals, preference.*, constraint.*, tax.*, other
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
                if existing_tables >= _TABLES:
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
        }

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
                latest = self._latest_ids(client_id) if decisions else {}
                return {
                    "client": {
                        "id": client["id"],
                        "display_name": client["display_name"],
                        "revision": client["revision"],
                    },
                    "facts": [self._fact_from_row(row) for row in facts],
                    "decisions": [self._decision_from_row(row, latest) for row in decisions],
                }

    def _normalize_fact(self, raw: Any) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(raw, Mapping):
            raise ValidationError("each fact must be an object")
        if any(not isinstance(field, str) for field in raw):
            raise ValidationError("fact field names must be strings")
        allowed = {"key", "value", "source", "confidence", "expires_on", "merge"}
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
        if kind == "inference" and confidence != "inferred":
            raise ValidationError(f"{key}: an inference source must carry inferred confidence")
        warnings = []
        if kind in {"document", "web"} and _is_policy_key(key) and confidence != "inferred":
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
        }, warnings

    def _receipt(
        self, client_id: str, revision: int, request_id: str | None, replayed: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        client = self._client_row(client_id)
        written = self._db.execute(
            "SELECT id, key, confidence, expires_on, source_kind FROM facts "
            "WHERE client_id = ? AND revision = ? ORDER BY key",
            (client_id, revision),
        ).fetchall()
        return {
            "client": {"id": client_id, "revision": client["revision"]},
            "written": [
                {"key": row["key"], "id": row["id"], "confidence": row["confidence"],
                 "expires_on": row["expires_on"], "source_kind": row["source_kind"]}
                for row in written
            ],
            "write_result": {
                "request_id": request_id, "resulting_revision": revision, "replayed": replayed,
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
        requires the revision the caller read.
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
                    row["key"]: row
                    for row in self._db.execute(
                        f"SELECT f.* FROM facts f JOIN (SELECT key, MAX(revision) revision "
                        f"FROM facts WHERE client_id = ? AND key IN ({','.join('?' for _ in keys)}) "
                        f"GROUP BY key) latest ON latest.key = f.key AND latest.revision = f.revision "
                        f"WHERE f.client_id = ?",
                        (client_id, *keys, client_id),
                    )
                }
                for fact in normalized:
                    prior_fact = current.get(fact["key"])
                    if fact["merge"] and prior_fact is not None:
                        fact["value"] = merge_patch(
                            json.loads(prior_fact["value_json"]), fact["value"], fact["key"]
                        )
                        if prior_fact["confidence"] == "inferred" or is_stale(dict(prior_fact)):
                            warnings.append(
                                f"{fact['key']} was merged into an inferred or past-review value "
                                f"observed on {prior_fact['observed_on']}; reconfirm the unchanged parts"
                            )
                    elif prior_fact is not None and expected_revision is None:
                        raise ValidationError(
                            f"{fact['key']} already has a value; send it with merge=true to "
                            f"update fields, or pass expected_revision={current_revision} to replace it"
                        )
                    elif fact["merge"] and isinstance(fact["value"], dict):
                        fact["value"] = merge_patch({}, fact["value"])
                new_revision = current_revision + 1
                self._db.execute(
                    "UPDATE clients SET revision = ? WHERE id = ?", (new_revision, client_id)
                )
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
                result = self._receipt(client_id, new_revision, request_id, False, warnings)
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
    "DEFAULT_REVIEW_DAYS",
    "REVIEW_DAYS",
    "SCHEMA_VERSION",
    "ClientExistsError",
    "ClientNotFoundError",
    "DecisionNotFoundError",
    "IneligibleEvidenceError",
    "RequestConflictError",
    "StaleRevisionError",
    "StoreError",
    "ValidationError",
    "WealthStore",
    "is_stale",
    "merge_patch",
    "review_days",
]
