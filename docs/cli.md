# CLI and MCP reference

The CLI and MCP tools share the same service and task catalog.
See [agent setup](agent.md) for interactive chat.

## Surface

| MCP tool | What it does |
| --- | --- |
| `wealth_context` | Discover tasks without a client, or retrieve a compact intent/query-specific client packet |
| `wealth_remember` | Atomically record sourced facts or corrections at an expected revision |
| `wealth_run` | Run one catalog task, optionally against client context and optionally save its validated result |
| `wealth_recall` | Retrieve bounded evidence by query; optional semantic vectors must be supplied and identified by the host |
| `wealth_decision` | Propose, accept, or dismiss an evidence-bound decision; acceptance is not execution |
| `wealth_client` | Create, inspect, export, forget, or attach a host-provided vector index entry for one explicit client |

The CLI uses the same names without `wealth_`: `context`, `remember`, `run`,
`recall`, `decision`, and `client`. Existing compatibility aliases remain
available. Commands accept one JSON object from `--input request.json` or stdin;
`--db` overrides `WEALTH_DB`.

Compound operations use a small action envelope:

| Operation | Action | `inputs` |
| --- | --- | --- |
| `decision` | `propose` | `title`, `rationale`, `expected_revision`, `evidence_ids`, optional `alternatives` |
| `decision` | `accept` / `dismiss` | `decision_id`, `expected_revision` |
| `client` | `create` | `display_name` |
| `client` | `inspect` | optional `detail: current|history` and `key` for history |
| `client` | `export` | none |
| `client` | `forget` | `confirm_client_id` matching the requested client |
| `client` | `index` | `fact_id`, host-supplied `embedding`, exact `model` identifier |

## A client journey from the CLI

Provision the instance’s profile during setup. The assistant then remembers relevant
explicit facts automatically; it respects requests not to save a detail.
The following examples use a fictional profile:

```sh
printf '%s' '{"action":"create","client_id":"ana","inputs":{"display_name":"Ana"}}' \
  | uv run wealth client
```

Record a sourced fact at revision 0:

```sh
printf '%s' '{
  "client_id":"ana",
  "expected_revision":0,
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

Use `wealth_client` / `wealth client` to inspect current state or key history,
export full private history, and delete one named client with the same ID as
explicit confirmation.

## Monitoring

Nothing starts automatically. `wealth watch` runs an explicitly started polling
loop for one client’s opt-in rules. It prints only changed monitor events to
stdout. It does not send notifications, place trades, move money, or keep running
after the process stops. A one-shot `monitor` task is available through
`wealth_run` for hosts that provide their own scheduler.

```sh
uv run wealth watch --client ana --interval 300
```
