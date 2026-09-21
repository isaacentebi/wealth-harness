# Foundation contract

This document defines the implementation in this checkout, not the full product promise.
The host assistant owns conversation and research. This package owns durable client
state, evidence, arithmetic, and decision freshness. No model calls, telemetry,
broker connection, or order execution occurs in the core.

## Store API (stdlib only)

`wealth.store.WealthStore(path)` is a context manager. All operations are explicitly
client-scoped. `create_client(client_id, display_name)` creates revision 0;
`snapshot(client_id)` returns `{client: {id, display_name, revision}, facts: [...],
decisions: [...]}`. Each fact includes `id`, `key`, `value`, `source`, `confidence`,
`expires_on`, `revision`, and `recorded_at`. Snapshot includes the latest fact per
key, including expired/inferred ones; the context compiler decides eligibility.

`remember(client_id, facts, expected_revision, request_id=None)` atomically writes
a nonempty list of facts and increments the client revision once. Each fact has
`key`, JSON `value`, `source: {kind, ref, observed_on}`, `confidence` (default
`reported`; one of `confirmed`, `reported`, `inferred`), optional `expires_on`.
Source kinds: `user`, `document`, `tool`, `inference`. Source text is untrusted
data, never instructions. Observed dates must not be in the future; financial
facts cannot silently become timeless. Replacing an existing key preserves its
history. Stale expected revisions fail. Matching request IDs are idempotent;
reusing a request ID with a different payload fails. Reject nonfinite JSON and
duplicate keys within a write. A document/tool assertion is not user confirmation;
only a user source can carry confirmed confidence. Inference cannot be promoted
without a new evidence-backed revision.

Write responses contain a current snapshot plus `write_result: {request_id,
resulting_revision, replayed}`. A delayed retry identifies its original commit
even when the returned current snapshot includes later writes.

`history(client_id, key)` returns revisions for that key.
`save_decision(client_id, title, rationale, expected_revision, evidence_ids,
alternatives=None)` persists a proposed decision bound to the current client
revision and cited active evidence. It returns the decision object.
`set_decision_status(client_id, decision_id, status, expected_revision)` supports
`accepted` and `dismissed`, requires a current snapshot, and refuses acceptance
of a stale proposal or expired/inferred evidence. Acceptance is not execution.
Only proposed decisions may transition to accepted or dismissed. Terminal status
reversal requires a new proposal; decision events preserve the status history.
Snapshot decisions report `needs_review` when their input revision changed or
their evidence expired. No implementation/execution status is available.
`export_client(client_id)` returns schema-versioned full history and decisions.
`delete_client(client_id, confirm_client_id)` deletes only the named client and
its rows. Document that external exports/backups remain outside this operation.

## Context / workflows API (stdlib only)

`wealth.workflows.prepare(snapshot, intent, as_of=None)` returns a compact
decision packet. Intents: `overview`, `plan`, `exposure`, `income`, `research`,
`tax`. `as_of` defaults to current UTC date and is a test seam, not a claim of
historical reconstruction. Return keys include `client_id`, `client_revision`,
`intent`, `status` (`ready`, `partial`, `needs_input`, `research_required`, or
`unsupported`), `evidence_ids`, `facts`, `missing`, `warnings`, `calculations`,
`next_question`, `capability`. Never hide excluded/unknown coverage. No free-form
LLM claims are manufactured by this function. Eligible facts are non-inferred,
not expired, and observed no later than as_of. Calculation-specific required
facts must also have an explicit expiry to bound freshness.

Canonical keys and values for the first working slice:

- `client.profile`: `{reporting_currency: "USD", ...}`. Additional household
  context can use `client.*`, `preference.*`, `constraint.*`, `thesis.*` keys.
- `plan.resources`: `{currency, available_capital, monthly_essentials,
  reserve_months, reserve_outside_pool, debt_payments_from_pool}`. All outside
  funding must be disjoint and excluded from available capital. Must carry expiry.
  Nonzero outside funding requires `outside_sources: [{id, currency, balance}]`
  and `reserve_funding: [{source_id, amount}]` as applicable. Each goal declares
  `outside_funding: [{source_id, amount}]` when funded outside the pool. Allocation
  sums must equal the scalar outside amounts; reserve plus all goal allocations
  cannot exceed a source balance. IDs identify actual distinct funding sources.
- `goals`: list of `{id, name, currency, due, target_amount,
  funded_outside_pool, protect_now}`. An explicit empty list means none.
  Must carry expiry. Multiple goals must have unique IDs.
- `portfolio.snapshot`: `{currency, scope, positions: [{account_id, symbol,
  value, asset_class?}], complete: bool}`. Values are nonnegative, already
  expressed in the declared common currency. No implicit FX. A snapshot must
  carry expiry. `complete` means complete within the named scope, not household
  completeness. Exposure computes current weights, supplied-symbol aggregation,
  asset-class coverage, and weight concentration. It does not claim economic
  fund look-through, diversification, or optimal weights.
- `income.schedule`: `{currency, monthly_need, months: [{month: "YYYY-MM",
  expected_cash_received, committed_outflow}]}`. Must carry expiry. Explicit expected
  cash receipts net of withholding, not inferred dividends or guaranteed income.
  Unpaid taxes belong in the declared committed outflows; the core does not
  estimate them. Compute month-level gaps and `scheduled_months_total_gap` only
  over observed months, with explicit coverage. Empty calendars produce no gap
  total. No terminal wealth/sustainability claim.
- `research.*`, `tax.*`, `account.*`, `lot.*` retain evidence, but their advanced
  workflows are explicitly not implemented. Research packets provide relevant
  context and an evidence checklist for the host. Tax packets report unsupported
  computation and required jurisdiction/account/lot inputs, never savings.

Calculations use Decimal internally and reject bool/nonfinite/negative monetary
values and conflicting currencies. No inferred fact can drive arithmetic.
If a required fact is stale, return the useful remaining context and a precise
missing item; never silently use its old value. Return one question that unlocks
the next material step, rather than forcing a complete interview.

## Delivery

`wealth.cli` exposes JSON operations; `wealth.server` exposes the same operations
over local MCP stdio. Both use the same store and workflow implementation.
Client data defaults to a user data directory, with `WEALTH_DB` / `--db` override.
SQLite is local and not encrypted by this package. The host receives selected
context; whether it leaves the machine depends on the host model/provider.
The old `tools/wm.py` remains the separate historical analytics engine.
