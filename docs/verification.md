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
not every third-party harness. No human usability study or comprehensive
conversation-quality evaluation has been performed. The host still owns
conversational judgment and explanations.


## Real agents — 2026-09-21 UTC

Codex CLI 0.153.4 reported an active ChatGPT login. No API key or OAuth token
was copied into Wealth. Bounded live runs used the actual Wealth MCP server:

- **GPT-5.6 Sol:** retrieved a fictional client's context, ran `plan`, and
  correctly returned $226,000 total uncommitted capital after a $24,000 reserve
  and $50,000 home goal from a $300,000 pool.
- **GPT-5.6 Luna:** retrieved the same client, saved the requested goal correction
  to $100,000, and reran `plan`, returning $176,000. A direct database read and
  deterministic recalculation verified the persisted correction. Luna corrected
  its initial local-date provenance to the UTC date; the launcher now explicitly
  supplies the current UTC date.
- **Actual `wealth-agent` entrypoint with Sol:** discovered and called Wealth
  tools and distinguished the $226,000 total investment budget from $26,000
  additional cash when $200,000 was already invested.

These are small live integration checks, not a quality guarantee for every
financial question. Raw event logs and test databases remain local under ignored
`private/`. The terminal launcher uses bounded conversation history and persistent
Wealth memory, supports Sol/Luna, and leaves the user's Codex configuration intact.

Launcher regressions cover command isolation, surfaced failures, rejection of an
incomplete event stream, and demo persistence. The integrated local suite passed
with 345 tests; the final canonical demo also passed its focused exposure check.

## Adversarial review and progressive onboarding (2026-09-21)

Requested native routed reviews completed with SWE-2 (runtime reliability),
Opus 5 (conversation, memory, onboarding), and GLM-5.3 Flash (financial semantics).
Their findings were checked against current code; these are scoped reviews, not
certification of every financial method.

Changes include automatic explicit-fact memory, a shared literal-language policy
with no metaphors or analogies, create/resume onboarding, canonical memory
contracts in client context, a bounded known-key index, full-record retrieval
before structured updates, and a defined review deadline for new financial facts.
Incomplete goals can be saved without fabricated amounts and cannot produce
calculations until required inputs are available.

Plan results now separate total uncommitted capital from additional cash available
to invest, using explicit unrestricted cash in the declared capital scope. Unknown
cash returns null. Household reconciliation is not implied. Reserve, debt and
protected goals must describe distinct commitments; the tool does not infer
semantic duplicates from goal names.

Review recommendations not adopted: exposing arbitrary process stderr; treating
malformed event streams as successful; silently reaccepting stale financial
decisions; deduplicating goals because their amounts happen to match; and adding
unsourced mortgage estimates to conversation examples. An alleged f-string brace
issue was invalid: interpolated string contents are not evaluated as Python.

Live checks used fictional profiles with the actual Codex runtime, Sol and Luna,
and Wealth MCP. Automatic writes and cross-session recall were observed; a
considered property price was not promoted to a committed budget. Early runs
exposed invented fact keys and excessive recap text, which informed the changes.
Behavior still depends on the host model following the contract; prompt text is
not a guarantee that every future response will comply.

Final checks: `uv run pytest -q` passed all 350 existing and focused regression
checks; source/wheel build and the offline complete journey passed. The final
live onboarding run saved canonical `client.profile` and incomplete `goals`,
used the 30-day review deadline, invented no budget, and asked one financing
question. Interrupted demo initialization is now repaired without replacing
recorded client state.
