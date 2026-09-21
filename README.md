# Wealth

A personal financial assistant with persistent memory, investment research and
Python financial analysis. Use the local chat or connect the tools to your own
agent through MCP. Each assistant instance serves one person.

## Start chatting

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and an installed Codex CLI.
For the local assistant, sign into Codex with your ChatGPT account:

```sh
uv sync
codex login
uv run wealth-chat --client my-profile --model sol
```

Open **http://127.0.0.1:8765/**. Onboarding starts with income, spending, savings,
investments and debt. Relevant facts are remembered automatically. Web search is
on; response depth defaults to Fast and can be changed to Balanced or Deep in the
chat. Progress streams while the assistant works, and a response can be stopped.

For a fictional terminal demo, run `uv run wealth-agent --demo --model sol`.
Use `--model luna` for Luna. [Setup, memory and fresh test sessions](docs/agent.md).

## Connect your own agent

Wealth's MCP server needs no model API key. The host supplies the model and web
search; it can use OpenRouter or another provider. Give it [SKILL.md](SKILL.md)
and configure the local server:

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

Provision one profile for the instance. Discover task schemas with
`wealth_context(intent="overview")` without a profile ID; include the ID for
personal recall. See the [CLI and MCP reference](docs/cli.md).

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

## Repository guide

| File or directory | Purpose |
| --- | --- |
| [docs/agent.md](docs/agent.md) | Run chat, terminal or a fresh test profile |
| [docs/cli.md](docs/cli.md) | CLI/MCP operations and examples |
| [docs/architecture.md](docs/architecture.md) | Runtime, storage and module boundaries |
| [docs/verification.md](docs/verification.md) | Reproducible checks and what they establish |
| [docs/open-source.md](docs/open-source.md) | Dependencies and reuse boundaries |
| [SKILL.md](SKILL.md) | Portable instructions for an external host |
| [wealth/instructions.md](wealth/instructions.md) | Shared assistant conversation policy |
| [references/playbook.md](references/playbook.md) | Financial interpretation guidance |
| `wealth/` | Runtime and financial tools |
| `examples/` | Fictional executable journeys |
| `tests/`, `tools/test_*.py` | Regression checks, including the legacy engine |

Superseded plans and development handoffs are available in Git history.
Private profiles, databases and local test outputs are ignored by Git.

## Development

```sh
uv sync --extra dev
uv run pytest -q
uv run python -m examples.complete_journey
uv build
```
