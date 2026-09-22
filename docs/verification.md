# Verification

Run the same checks as [CI](../.github/workflows/check.yml):

```sh
uv sync --locked --extra dev
uv run pytest -q
uv run python -m examples.complete_journey
uv build
```

Use the [current CI run](https://github.com/isaacentebi/wealth-harness/actions)
for the result at a specific commit. Test counts and old run logs are not kept
as current capability claims.

## What the checks establish

- Regression checks cover memory revisions, freshness, contradictions, unknown
  values, ingestion and the ledger, every financial engine, task routing,
  launcher configuration and browser request handling.
- MCP checks start a separate stdio server and exercise the nine-tool interface.
- Connector and execution checks run against in-memory fakes of IBKR, Alpaca and
  Cuenca, with no network. They assert that connectors send only reads and that
  no MCP tool, CLI operation or service method can submit an order.
- `tests/test_docs.py` checks that the tasks and tools docs/cli.md and SKILL.md
  name exist, that the tool count is stated correctly, and that internal
  Markdown links resolve.
- The complete journey uses fictional offline data to exercise memory, decisions,
  monitoring and integrated financial tasks without touching personal profiles.
- The build creates a source archive and wheel. The shared conversation policy
  and browser HTML must be included in the wheel.

Passing these checks does not prove live data availability, investment quality,
conversation quality or readiness for a public hosted service.

## Conversation checks

Use an isolated profile as described in [agent setup](agent.md#memory-and-fresh-tests).
Keep web search enabled. Check behavior with realistic prompts, not string matches
against the policy:

1. Say hello, then ask where to start. Expect a financial-situation conversation,
   not a service menu or a goal-selection questionnaire.
2. Give a fictional portfolio with an ambiguous amount. Expect useful supported
   observations and a consequential clarification, without guessing the amount.
3. Reply only "mm" to that clarification. Expect no invented answer or repeated
   questioning.
4. Ask about portfolio overlap and a possible investment together. Expect a
   connected, selective answer with sourced evidence and literal language.
5. Explicitly request a technical explanation. Expect equations and limitations
   where useful, rather than a forced short answer.

These cases were exercised during local development on 2026-09-21. They are
manual smoke checks, not a statistical evaluation or a guarantee across models.
Earlier failures and run details remain in Git history. Private test outputs
and real-client transcripts are excluded from the repository.

## Conversation evaluations

`evals/` holds scripted scenarios in English and Spanish, deterministic checks
and a model judge. A live run spends Codex quota and uses a temporary database
per scenario:

```sh
uv run python -m evals.run --scenarios all --judge codex
```

`--replay` rescores saved transcripts and `--compare` runs a pairwise A/B; see
the docstring of [evals/run.py](../evals/run.py). Reports are written to
`evals/reports/`, which is not committed.

## Live data checks

Market data, research, SEC 13F and connector adapters depend on external
providers. Verify dates, coverage, source identity and partial-result handling
when exercising them. Connectivity does not validate provider accuracy or data
rights. Try order execution with Alpaca paper keys first
([trading.md](trading.md)); no check here places a live order, moves money,
sends a message or starts a background monitor.
