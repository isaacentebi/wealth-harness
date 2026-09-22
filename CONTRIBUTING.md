# Contributing

Thanks for helping with Wealth. This page is where a new contributor starts.

## Setup

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/isaacentebi/wealth-harness.git
cd wealth-harness
uv sync --extra dev
```

The engines, the MCP server and the JSON CLI need no model. Only the local chat
(`wealth-chat`) and the terminal assistant (`wealth-agent`) need a signed-in
Codex CLI ([docs/agent.md](docs/agent.md)).

## Checks

Run what CI runs ([.github/workflows/check.yml](.github/workflows/check.yml))
before you open a pull request:

```sh
uv run pytest -q
uv run python -m examples.complete_journey
uv build
```

CI runs on Python 3.11, 3.13 and 3.14. What each check establishes is in
[docs/verification.md](docs/verification.md).

## Rules for changes

- **No real model calls in tests.** Tests fake the Codex process, the model
  judge and market, filing and broker providers. Nothing in `pytest` may spend
  model quota, need a login or depend on a live service. Live conversation evaluations (`evals/`) are run by
  hand and are not part of the suite.
- **No real brokers or personal data.** Connector and execution tests use
  in-memory fakes of IBKR, Alpaca and Cuenca. Fixtures and examples are
  fictional. Never commit a statement, a transcript of a real person or a key.
- **Figures come from code.** A number the person sees is computed by a
  deterministic engine with its sources and assumptions, not by the model.
  A new calculation is a catalog task with a test, not a prompt instruction.
- **Only the person places an order.** No MCP tool, CLI operation or service
  method may submit, confirm or cancel an order. See
  [docs/trading.md](docs/trading.md) before touching execution.
- **Docs change with the code.** When a change adds, renames or removes a task,
  tool or command, update [SKILL.md](SKILL.md), [docs/cli.md](docs/cli.md) and
  [docs/architecture.md](docs/architecture.md) in the same change, and the
  OpenClaw skill in `integrations/openclaw/skills/wealth/` when a tool changes.
  `tests/test_docs.py` and `tests/test_openclaw_skill.py` catch names and
  counts that drift and links that break.
- **One policy.** [wealth/instructions.md](wealth/instructions.md) is the
  conversation policy; [SKILL.md](SKILL.md) is its condensed form for MCP hosts
  and must not contradict it.

## Where things are

1. [README.md](README.md): what the product does and its boundaries.
2. [docs/architecture.md](docs/architecture.md): module map, data flow,
   invariants and trust boundaries.
3. [docs/cli.md](docs/cli.md): every task, command and MCP tool with a working
   example.
4. [docs/trading.md](docs/trading.md): order execution and its threat model.
5. [docs/openclaw.md](docs/openclaw.md): text channels through OpenClaw.
6. [docs/mexico-investing-facts.md](docs/mexico-investing-facts.md): verified
   Mexico rules the engines cite.
7. [docs/notes/](docs/notes/README.md): design notes and research behind past
   decisions.

Security issues go through [SECURITY.md](SECURITY.md), not public issues.
