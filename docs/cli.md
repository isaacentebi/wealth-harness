# CLI and MCP reference

The CLI and MCP tools share the same service and task catalog.
See [agent setup](agent.md) for interactive chat.

## Surface

| MCP tool | What it does |
| --- | --- |
| `wealth_context` | Discover tasks without a client, or retrieve a compact intent/query-specific client packet |
| `wealth_remember` | Atomically record sourced facts, corrections or `merge` patches; returns a compact receipt |
| `wealth_run` | Run one catalog task, optionally against client context and optionally save its validated result |
| `wealth_recall` | Retrieve bounded evidence by query; optional semantic vectors must be supplied and identified by the host |
| `wealth_decision` | Propose, accept, or dismiss an evidence-bound decision; acceptance is not execution |
| `wealth_inspect` | Read-only: current facts (optionally filtered by `key`/`keys`), one key's `history`, or a full `export` |
| `wealth_client` | Create a client or attach a host-provided vector index entry |

`wealth_context`, `wealth_recall` and `wealth_inspect` are annotated read-only.
No MCP tool is destructive: deleting a client is CLI-only (`client` action
`forget`), so a model cannot erase a profile without the person running it.

The CLI uses the same names without `wealth_`: `context`, `remember`, `run`,
`recall`, `decision`, and `client` (whose `inspect`/`export` actions correspond
to `wealth_inspect`). Existing compatibility aliases remain
available. Commands accept one JSON object from `--input request.json` or stdin;
`--db` overrides `WEALTH_DB`.

Compound operations use a small action envelope:

| Operation | Action | `inputs` |
| --- | --- | --- |
| `decision` | `propose` | `title`, `rationale`, `expected_revision`, `evidence_ids`, optional `alternatives` |
| `decision` | `accept` / `dismiss` | `decision_id`, optional `expected_revision` |
| `client` | `create` | `display_name` |
| `client` | `inspect` | optional `detail: current|history|export`; `key` or `keys` filters current facts; `key` is required for history |
| `client` | `export` | none; full private history including superseded facts and decision events |
| `client` | `forget` | `confirm_client_id` matching the requested client (CLI only) |

Errors name the offending field and the expected inputs. A stale revision error
reports the current revision.

## Facts

A fact has `key`, `value`, `source` (`kind`, `ref`, `observed_on`), optional
`confidence` (default `reported`), optional `expires_on` and optional `merge`.

- `source.kind`: `user` for what the person said, `document` for a supplied file,
  `web` for a page (`ref` is the URL), `tool` for a Wealth result, `inference`
  for an interpretation. Goals, profile, preferences, constraints and tax
  profile from `document`/`web` are stored as `inferred` until the person
  confirms them.
- `expires_on` defaults to `observed_on` plus the review horizon for the key
  (`store.REVIEW_DAYS`: 30 days for holdings and balances, 90 for plan
  resources, income schedules and research, 180 for theses, 365 otherwise).
  Past-review facts remain in recall and context, marked stale, and are
  excluded from calculations and decisions.
- Without `expected_revision`, a write may add new keys or apply `merge: true`
  patches (RFC 7386 for objects; lists of objects merge by `id`). Replacing an
  existing value wholesale requires `expected_revision`.
- Government IDs, account or card numbers, addresses and credentials are
  rejected.

`remember` returns `client`, `written` (key, id, confidence, expiry, source
kind), `write_result` and `warnings`; use `inspect` to read values.
| `client` | `index` | `fact_id`, host-supplied `embedding`, exact `model` identifier |

## A client journey from the CLI

Provision the instance’s profile during setup. The assistant then remembers relevant
explicit facts automatically; it respects requests not to save a detail.
The following examples use a fictional profile:

```sh
printf '%s' '{"action":"create","client_id":"ana","inputs":{"display_name":"Ana"}}' \
  | uv run wealth client
```

Record a sourced fact (a new key needs no revision):

```sh
printf '%s' '{
  "client_id":"ana",
  "request_id":"ana-profile-1",
  "facts":[{
    "key":"client.profile",
    "value":{"reporting_currency":"USD"},
    "source":{"kind":"user","ref":"conversation 2026-09-20","observed_on":"2026-09-20"},
    "confidence":"confirmed"
  }]
}' | uv run wealth remember
```

Recall relevant context, then run a calculation. Direct inputs override memory
for that call. Saving requires `save_as`, `client_id`, and `expires_on`;
validated imports may save `household`, while other results use an
`analysis.<name>` or `research.<symbol>` key.

```sh
printf '%s' '{"client_id":"ana","intent":"exposure","query":"current household exposure"}' \
  | uv run wealth context

printf '%s' '{
  "task":"analyze",
  "client_id":"ana",
  "inputs":{"currency":"USD","weights":{"SPY":0.6,"BND":0.4},"benchmark":"SPY","years":5}
}' | uv run wealth run
```

Proposals cite eligible evidence and the current client revision. Resolve them
with a separate call; stale or inferred evidence cannot support acceptance.

```sh
printf '%s' '{
  "action":"propose","client_id":"ana",
  "inputs":{"title":"Keep a reserve","rationale":"The home goal has a hard date","expected_revision":1,"evidence_ids":["REPLACE_WITH_FACT_ID"],"alternatives":[]}
}' | uv run wealth decision
```

Update one field later without re-reading the revision:

```sh
printf '%s' '{"client_id":"ana","facts":[{"key":"client.profile","merge":true,
  "value":{"spending_currency":"MXN"},
  "source":{"kind":"user","ref":"conversation 2026-09-21","observed_on":"2026-09-21"}}]}' \
  | uv run wealth remember
```

Inspect selected keys, one key's history, or export everything:

```sh
printf '%s' '{"action":"inspect","client_id":"ana","inputs":{"keys":["client.profile","goals"]}}' | uv run wealth client
printf '%s' '{"action":"inspect","client_id":"ana","inputs":{"detail":"history","key":"goals"}}' | uv run wealth client
printf '%s' '{"action":"export","client_id":"ana"}' | uv run wealth client > ana-export.json
```

Delete one named client (CLI only). Run it from an interactive terminal; it asks
you to type the client ID followed by `DELETE`. Piped or scripted input is
refused, so an agent with shell access cannot delete a profile:

```sh
uv run wealth forget --client ana
```

## Monitoring

Nothing starts automatically. `wealth watch` runs an explicitly started polling
loop for one client’s opt-in rules. It prints only changed monitor events to
stdout. It does not send notifications, place trades, move money, or keep running
after the process stops. A one-shot `monitor` task is available through
`wealth_run` for hosts that provide their own scheduler.

```sh
uv run wealth watch --client ana --interval 300
```
