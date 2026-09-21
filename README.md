# Wealth

**A portable wealth capability that remembers the client, keeps evidence and
decisions connected, and brings serious financial analysis into any agent.**

Wealth runs locally behind six MCP tools or the matching JSON CLI. SQLite is
authoritative for client memory. The host agent owns conversation and source
work; Wealth owns provenance, calculations, freshness, and decision history.

## Install and connect

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Runtime dependencies
are in the default environment:

```sh
uv sync
```

Start the local stdio server:

```sh
WEALTH_DB=/absolute/private/path/wealth.sqlite3 uv run wealth-mcp
```

Generic MCP configuration:

```json
{
  "mcpServers": {
    "wealth": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/wealth-mgmgt", "run", "wealth-mcp"],
      "env": {"WEALTH_DB": "/absolute/private/path/wealth.sqlite3"}
    }
  }
}
```

Give the host [SKILL.md](SKILL.md). Calling `wealth_context` without a client
returns the live task catalog, examples, and boundaries. Set `intent` to a task
name to retrieve only that task’s contract. The host does not need
the implementation manual in its prompt.

## Talk to a real agent

If Codex CLI is installed and you are signed in with ChatGPT:

```sh
uv run wealth-agent --demo --model sol
```

Use `--model luna` to switch models. Ask “How much can I invest while protecting
my home goal?”, then change the goal and ask it to remember. The fictional
client's Wealth memory persists across sessions and models. No extra API key is
needed for this local path. [Agent setup and details](docs/agent.md).

## Model provider

Wealth needs no LLM API key. Your host assistant can use OpenRouter or another
provider and call Wealth through MCP. See [provider setup](docs/providers.md).

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

## What it can run

| Area | Tasks | Current capability |
| --- | --- | --- |
| Household | `import`, `exposure` | Reconciled people/accounts/positions/lots; NAV and liquidity; FX and fund look-through coverage; overlap and targets |
| Markets | `analyze`, `stress`, `compare`, `construct`, `factors` | Historical risk, explicit stress cases, same-sample comparison, constrained equal/inv-vol/min-var/risk-parity/HRP/CVaR/Black–Litterman construction, optional walk-forward validation |
| Research | `research`, `value` | Dated source packets, prior-case deltas, company/fund metrics, explicit DCF and multiples scenarios |
| Household policy | `plan`, `calendar`, `project`, `income`, `ladder` | Dated goals and reserves, cash calendar, seeded projections, sequence-risk income comparison, liability cash-flow matching |
| Tax | `tax` | Explicit scenarios for US federal taxable securities and Mexico Article 129 qualifying listed shares |
| Monitoring | `monitor` | Caller-driven evaluation of opt-in review, expiry, drift, goal, threshold, and thesis rules |

Get exact required fields and a valid example for every task:

```sh
printf '{}' | uv run wealth context
```

## A client journey from the CLI

Create a client only after the user chooses persistent memory:

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

## Boundaries

- No brokerage connection, order placement, transfer, external message, or
  claim that an accepted decision was executed.
- Tax scope is limited to the implemented US federal taxable-security and
  Mexico Article 129 scenarios. It does not prepare a return, cover every tax,
  or replace jurisdiction-specific professional review.
- Research can use supplied sources or an explicit live adapter. Aggregator data
  is identified; stale evidence and missing coverage remain visible.
- Black–Litterman uses only explicit caller views. Construction always reports a
  stable baseline; walk-forward evidence is optional and does not prove an edge.
  Black–Litterman validation requires a dated belief schedule.
- Recall has bounded keyword/concept matching. Wealth creates no embeddings.
  Semantic ranking occurs only when the host supplies compatible vectors and an
  exact model identifier; SQLite remains authoritative.
- Unknown is not zero. Inference is not fact. Stale financial evidence is
  excluded from calculations and cannot support acceptance.
- Local SQLite is plaintext. Selected context may reach the host’s model
  provider. Client scoping prevents accidental joins; it is not public-service
  authorization. External exports and backups survive local deletion.

Run the complete fictional, offline client journey:

```sh
uv run python -m examples.complete_journey
```

See the [product specification](docs/product.md) and
[financial interpretation guide](references/playbook.md).

## Development

```sh
uv sync --extra dev
uv run pytest -q
```
