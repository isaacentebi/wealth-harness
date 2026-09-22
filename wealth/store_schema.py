"""SQL DDL for the SQLite store: base tables, the ledger, contradictions, orders, market cache
and conversations.  ``wealth.store`` owns when each script runs (creation and migrations).
"""

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
