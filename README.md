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

### OpenClaw (WhatsApp, Telegram, iMessage, Signal)

To talk to Wealth from your own [OpenClaw](https://github.com/openclaw/openclaw)
assistant, run `integrations/openclaw/install.sh` (try `--dry-run` first) and
restart the gateway. It installs the skill, registers the MCP server and creates
a private data directory. Setup, text-channel onboarding, images and privacy
notes: [docs/openclaw.md](docs/openclaw.md).

## Connections

Account connections are optional and read-only. If you have the account and
its API keys, Wealth pulls it on demand; everyone else keeps uploading
statements, which go through the same proposal and confirmation. A sync never
saves anything by itself: it returns a proposal, and only the person's yes
(`ingest action=confirm`) records it. Keys live in the OS keychain or
environment variables and never reach the database, logs or the chat.

| Provider | How | What comes in |
| --- | --- | --- |
| Interactive Brokers | `ibkr_flex`: Flex Web Service token (keychain `wealth-ibkr-flex`) and an Activity Flex Query id | Positions with lots, trades, dividends, withholding, interest, fees, cash, FX, splits, NAV |
| Alpaca | `alpaca`: API key id and secret (keychain `wealth-alpaca`, accounts `key_id` and `secret`); paper with `WEALTH_ALPACA_PAPER=1` | Positions at average cost (Alpaca has no lots), fills, dividends and NRA withholding, interest, fees, cash transfers, equity history |
| Cuenca | `cuenca`: API key and secret issued by Cuenca (keychain `wealth-cuenca`, accounts `api_key` and `api_secret`) | MXN balance and apartados, SPEI (by clave de rastreo), card purchases categorised by merchant, ATM withdrawals, commissions |
| Vest | No API for individual users: upload the monthly statement, or a CSV with `preset=vest` (a generic layout, labelled as such) | Holdings and activity from the file |

Check what is configured with `ingest action=connector_status`. Setup steps
and sources are in the [CLI reference](docs/cli.md#connectors).

## What it can run

| Area | Tasks | Current capability |
| --- | --- | --- |
| Statements | `wealth_ingest` | PDF, CSV/XLSX export, image text or chat facts into a reconciled proposal; saved and posted to the ledger only after the person confirms |
| Household | `import`, `exposure` | Reconciled people/accounts/positions/lots; NAV and liquidity; ownership, lockups and redemption terms; FX and fund look-through coverage; overlap and targets |
| Ledger | `ledger`, `performance`, `spending`, `dca` | Holdings, lots, realized gains and income from transactions; statement reconciliation; time- and money-weighted returns; spending categories and investable surplus; DCA schedules, adherence and backtests |
| Markets | `analyze`, `stress`, `compare`, `construct`, `factors`, `sic_premium` | Historical risk with explicit benchmark, risk-free and fees; stress cases; same-sample comparison; constrained equal/inv-vol/min-var/risk-parity/HRP/CVaR/Black–Litterman construction with optional walk-forward validation; SIC price against the home market |
| Research | `research`, `value` | Dated source packets, prior-case deltas, company/fund metrics, DCF (stub periods, EV bridge, dilution) and multiples scenarios |
| Household policy | `plan`, `calendar`, `project`, `income`, `ladder` | Dated goals and reserves, cash calendar, seeded projections (parametric, Student-t, bootstrap, block bootstrap; nominal or real), taxed income comparison, liability cash-flow matching |
| Tax | `tax`, `mx_holdings`, `mx_interest`, `mx_deductions`, `mx_foreign`, `mx_calendar`, `estate` | US federal lots, wash sales, harvesting and 2025/2026 brackets with NIIT; Mexico Art. 129, real interest, deductions/PPR, foreign securities, tax calendar; US estate exposure for non-residents |
| Monitoring | `monitor` | Caller-driven evaluation of opt-in review, expiry, drift, goal, threshold, and thesis rules |

Get exact required fields and a valid example for every task:

```sh
printf '{}' | uv run wealth context
```

## Boundaries

- No order placement, transfer, external message, or claim that an accepted
  decision was executed. Account connectors (IBKR Flex, Alpaca, Cuenca) only
  read, only on request, and need the person's confirmation before anything is
  saved; no aggregator connector ships.
- Tax covers the scenarios listed above. It does not compute state taxes or
  AFORE/IRA internals, prepare a return, take filing positions, or replace
  professional review.
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
