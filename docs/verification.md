# Local verification — 2026-09-20

Version 0.2.0 is an integrated local package: six MCP tools and sixteen financial
tasks, with the same service behind the CLI. This record distinguishes offline
fixtures, protocol behavior, installation, and live provider checks.

## Checks

- `uv run pytest -q`: **341 passed in 7.92 seconds**. Existing analytics
  regressions remain; new coverage focuses on financial boundaries, cross-module
  journeys, memory integrity, and the real transport.
- `uv run python -m examples.complete_journey`: all eleven exercised domain
  outcomes returned ready using explicitly fictional offline data. The journey
  covers import, exposure, market analysis/construction, research/valuation,
  projection/income/ladder, and both supported tax jurisdictions.
- The same journey saves and recalls goals, preferences, holdings, and a thesis;
  preserves goal correction history; marks an accepted decision for review after
  correction; and emits no event on an unchanged repeat monitor check.
- The MCP test launches a separate stdio process, discovers exactly six strict
  tools, and exercises memory, calculations, decisions, corrections, monitoring,
  export, invalid arguments, and sanitized errors.
- `wealth watch --once` was exercised in a separate process. No background
  watcher or real-client schedule was started.
- `uv build` creates the source archive and wheel. Source packaging explicitly
  includes code/examples/docs and excludes existing personal portfolio artifacts.
- An isolated wheel installation outside the repository successfully discovers
  the six tools and sixteen tasks and runs a financial calculation through MCP.
- Intent routing retrieves canonical household evidence for exposure questions.
  Task-specific discovery returns only the requested task contract.
- A saved calculation cites only consulted memory and derives freshness from it;
  an unrelated expiring thesis does not shorten a plan's validity.

## Live provider checks

- Yahoo adjusted closes: a USD 60% SPY / 40% BND portfolio returned ready with
  502 prices from 2024-09-18 through 2026-09-18.
- Ken French three-factor regression: SPY returned ready over 467 observations
  from 2024-09-19 through 2026-07-31. Its factor window is stated separately from
  the newer price window; missing provider data does not report ready.
- The research adapter was exercised with MSFT company research and SPY fund
  research. MSFT returned data; SPY remained partial when the holdings date was
  unavailable. Aggregator data is identified as such.

These checks establish connectivity and observable behavior, not an independent
audit of provider data accuracy or future availability.

## Material review fixes

- Preserved unknown household sections through import/save/reload; excluded
  retirement/restricted assets from assumed liquid capital; made allocation
  conclusions indeterminate when missing assets can change the denominator.
- Kept income exposure separate from NAV while linking shared sector/country/
  currency labels to known asset exposure, without claiming causal correlation.
- Rejected future research evidence; linked indirect fund holdings to research.
- Required explicit return-level US tax facts and sale-date quotes; prevented
  duplicate Mexican lot sales and required scoped eligibility for each sale.
- Distinguished fulfilled withdrawals from positive terminal wealth; carried
  unpaid ladder obligations forward before labeling cash uncommitted.
- Corrected walk-forward initial turnover; required historically dated
  Black–Litterman beliefs; rejected mismatched cash currencies.
- Preserved thesis-monitor baselines across stale intervals; kept unchanged
  checks quiet; serialized monitor/index writes without revising financial facts.

## Practical boundaries

No live client portfolio was imported during this work, no broker was connected,
and no trade, transfer, external notification, or deployment occurred. Tax
coverage is US federal taxable securities and Mexican Article 129 qualifying
listed shares. Inputs, eligibility, rates, and account coverage remain explicit.

Semantic retrieval accepts host-supplied embeddings; the package does not
create them. SQLite is local plaintext. MCP was tested with the official SDK,
not every third-party harness. No human usability study or model-driven
conversation evaluation has been performed. The host still owns conversational
judgment and explanations.
