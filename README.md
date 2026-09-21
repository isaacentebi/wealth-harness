# Wealth

A personal financial adviser that runs on your own computer and remembers you.
It is for people living in Mexico or the United States who want one place to
understand their money: what comes in and goes out, what they own, what to
invest in, what they will owe in tax, whether retirement works, and what to do
next. It answers in Mexican Spanish or English, with the products, taxes and
institutions of where you live.

Every figure comes from deterministic Python code with its sources and
assumptions; the language model explains and decides what matters. Your data
stays in a local SQLite file.

## Quick start

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and the Codex CLI signed in
with your ChatGPT account (`codex login`).

```sh
uv sync
uv run wealth-chat --client me
```

Open **http://127.0.0.1:8765/**. For a fictional profile instead of your own,
add `--demo`. Models, depth, fresh test profiles and the terminal version:
[docs/agent.md](docs/agent.md).

## The chat and the You page

**The chat** starts with your situation: a few one-question cards (who you are,
income, spending, savings, debts, goals, risk) that you can answer, skip or
replace with a statement upload, then a first synthesis of where you stand.
After that, ask anything. Progress streams while it works, answers can be
stopped, and results arrive with charts and tickets drawn by the engine, not
the model. Attach a PDF, CSV or photo of a statement and it shows what the
statement means next to what you told it, then asks before saving.

**The You page** (`/profile`) shows everything Wealth knows about you, where each
fact came from (you, a statement, a calculation) and when it was last checked.
You can edit, confirm or delete any fact, see its history, and settle any
contradiction between what you said and what a document shows.

## What it can do

| Life area | What Wealth does (tasks) |
| --- | --- |
| Money in and out | Where money goes and what is investable, cash calendars, projections, withdrawal and liability matching, debt payoff, reserves and goals (`spending`, `calendar`, `income`, `project`, `ladder`, `debt_payoff`, `plan`) |
| What you own | Holdings, lots, gains and income from a transaction ledger, returns, exposure and overlap, household import, SIC premium (`ledger`, `performance`, `exposure`, `import`, `sic_premium`) |
| Investing | Investment policy, tax-aware rebalancing, asset location, recurring investing, portfolio analysis and construction, company and fund research (`policy_draft`, `policy_check`, `rebalance`, `asset_location`, `dca`, `compare`, `construct`, `analyze`, `factors`, `stress`, `research`, `value`) |
| Tax | US federal lots, wash sales and harvesting; Mexico Art. 129, real interest, deductions and PPR, foreign securities, tax calendar; US estate exposure for non-residents (`tax`, `mx_holdings`, `mx_interest`, `mx_deductions`, `mx_foreign`, `mx_calendar`, `estate`) |
| Retirement | IMSS Ley 73/97, AFORE and Modalidad 40; Social Security, contribution limits and withdrawal order; a readiness range (`retirement_mx`, `retirement_us`, `retirement_readiness`) |
| Protection | Insurance and estate gaps, life events, and guardrails for speculation, panic selling and scams (`protection_review`, `life_event`, `speculation_check`, `panic_check`, `scam_check`) |
| Reviews and nudges | What needs attention today, a weekly letter, a quarterly review, a fee audit, opt-in monitor rules (`today`, `weekly`, `quarterly_review`, `fee_audit`, `monitor`) |
| Following managers | Find a fund manager's SEC 13F filings, read and profile them, compare managers, size a mirror within your policy (`manager_search`, `manager_holdings`, `manager_profile`, `manager_compare`, `manager_mirror`) |
| Connections | Statement uploads, plus read-only syncs from Interactive Brokers, Alpaca and Cuenca, each saved only after you say yes (`wealth_ingest`: `ibkr_flex`, `alpaca`, `cuenca`) |
| Execution | An order ticket with pre-trade checks for Alpaca that you place yourself by tapping its card (`order_ticket`) |

`printf '{}' | uv run wealth context` lists every task with its inputs and a
runnable example.

## Boundaries

- **Connectors only read.** IBKR Flex, Alpaca and Cuenca are pulled when you
  ask, never in the background, and only send read requests. A sync returns a
  proposal; nothing is saved until you say yes. Keys live in the OS keychain or
  environment variables, never in the database, logs or chat.
- **Only you place an order.** The model can prepare an order ticket; it is
  placed only when you tap **Place orders** on its card in the local chat page.
  No tool, command or "yes" in chat places one. Paper trading is the default;
  live trading needs a server-side opt-in, a typed confirmation the first time
  and per-order and daily limits. Wealth never moves money or sends messages.
  Details and threat model: [docs/trading.md](docs/trading.md).
- **Not a licensed adviser.** Wealth gives analysis and decision support, sizes
  only as ranges from your own figures, and does not prepare returns or take
  filing positions. It refers you by where you live: a contador público or CPA
  for tax, an abogado, notario or attorney for legal matters, CONDUSEF for
  disputes with Mexican financial institutions, a licensed adviser for ongoing
  discretionary management.
- **Privacy.** Your profile, ledger and decisions live in a local, plaintext
  SQLite file. Statement uploads are deleted after they are saved (or after 30
  days), and ingestion masks account numbers and drops RFC, CURP and SSN. The
  model provider (OpenAI through Codex, or your own host's provider) sees the
  conversation, a short summary of your situation each turn, and the tool
  results the model reads. Market, research and 13F data come from public
  sources when you ask.

## Connect your own agent

Wealth is also a model-independent MCP server with nine tools; the host brings
the model and web search. Give the host [SKILL.md](SKILL.md) and configure:

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

To talk to Wealth over WhatsApp, Telegram, iMessage or Signal through
[OpenClaw](https://github.com/openclaw/openclaw), run
`integrations/openclaw/install.sh --dry-run`, then without `--dry-run`, and
restart the gateway: [docs/openclaw.md](docs/openclaw.md).

## Documentation

| Document | For |
| --- | --- |
| [docs/agent.md](docs/agent.md) | Running the chat and terminal assistant, fresh test profiles, runtime policy |
| [docs/cli.md](docs/cli.md) | Every CLI command and MCP tool, with an example; ingestion and connector setup |
| [docs/architecture.md](docs/architecture.md) | Module map, data flow, invariants and trust boundaries |
| [docs/trading.md](docs/trading.md) | Guarded order execution: checks, setup, threat model |
| [docs/openclaw.md](docs/openclaw.md) | Text-channel setup through OpenClaw |
| [docs/verification.md](docs/verification.md) | Checks, evaluations and what they establish |
| [docs/open-source.md](docs/open-source.md) | Dependencies and reuse |
| [SKILL.md](SKILL.md) | Condensed policy for an MCP host |
| [wealth/instructions.md](wealth/instructions.md) | The assistant's full conversation policy |
| [references/playbook.md](references/playbook.md) | Interpreting financial results |
| [HANDOFF.md](HANDOFF.md) | Where a new contributor starts |

## Development

```sh
uv sync --extra dev
uv run pytest -q
uv run python -m examples.complete_journey
uv build
```
