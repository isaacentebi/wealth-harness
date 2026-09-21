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

- Regression checks cover memory revisions, freshness, unknown values, financial
  calculations, task routing, launcher configuration and browser request handling.
- MCP checks start a separate stdio server and exercise the six-tool interface.
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

## Live data checks

Market-data and research adapters depend on external providers. Verify dates,
coverage, source identity and partial-result handling when exercising them.
Connectivity does not independently validate provider accuracy or data rights.
No smoke check authorizes trading, transfers, external messages or a background
monitor. Those execution capabilities are not provided by Wealth.
