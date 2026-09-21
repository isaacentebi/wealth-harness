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

## Local browser chat (2026-09-21)

`wealth-chat` serves a dependency-free HTML chat on loopback with origin and
session-token checks, serialized turns, retryable errors, safe text/source-link
rendering, and the same client memory as the terminal. Native live web search is
enabled by default in both launchers. Python analytics remain implemented tool
functions; arbitrary shell execution is not enabled.

Verified browser input → real Sol response → clickable Vanguard source link.
A separate live turn emitted an actual `web.search` event. HTTP regression
checks covered request rejection, busy handling, history and agent arguments.
The full suite passed 353 checks; wheel build includes the HTML and server.
Facts persist in SQLite; the browser transcript persists only for the running
server session. File upload is not implemented.

## Skill review (2026-09-21)

The active Wealth skill was shortened from 152 to 107 lines, keeping task routing,
automatic memory, revisions, evidence and decision boundaries. The interpretation
guide is conditional reading, with current scoped tax, calendar and simulation
capabilities described accurately. Unlinked theme notes are marked historical.
Catalog examples are explicitly fictional; SPY is classified as a fund and its
missing constituents produce partial coverage. Skill validation, that exposure
probe, all 355 checks and package build passed.


## Initial conversation refinement (2026-09-21; superseded below)

The shared policy now asks the assistant to supply analytical expertise, select
consequential findings, and learn personal context through useful work. Ownership
overlap, historical co-movement and economic exposure remain distinct. Ambiguous
amounts still need clarification; they do not block unrelated research. Experience
is not inferred from wealth or holding count.

This initial implementation delivered the policy through Codex developer instructions,
instead of embedding it in the user message. Native verbosity defaults to low
independently of the existing reasoning selector. Standalone MCP retains its
policy. The command regression parses the TOML override and verifies deduplication.

Real Sol/low probes used isolated fictional profiles with web search enabled:

- Mixed portfolio review and Micron valuation: an early low-verbosity attempt
  remained a long report. Policy relocation and refinement reduced the final
  probe to 295 words, retaining cash commitments, indirect ownership and valuation
  uncertainty. It still volunteered risk statistics and used a figurative finance
  label, so this is an improvement, not reliable conversational compliance.
- Ambiguous Costco amount: investigated fund overlap and asked a relevant amount
  clarification without inventing the position size. Neutral tone was imperfect.
- Explicit expert comparison: preserved a detailed technical response (2,318 words)
  under developer instructions and low verbosity, before the last synthesis edit.

These are conversational smoke checks, not an independent audit of every live
market claim or a statistical quality evaluation. Outputs remain in ignored
private/agent-smoke; no real-client transcript was committed. Full regression
suite: 355 passed. After the final wording edits, 12 agent/MCP checks passed;
package build and skill validation passed. Local chat reload preserved the
conversation, token and reasoning selection, verified through HTTP state.


## Conversation delivery repair (2026-09-21)

The initial developer override still inherited Codex's coding instructions and
AGENTS guidance. The launcher now uses `model_instructions_file` with the packaged
`wealth/instructions.md`, and `project_doc_max_bytes=0`. Standalone MCP reads the
same file. Native web search and the reasoning selector are unchanged. The wheel
was checked to contain the exact policy file.

Ordinary chat has a soft brevity and paragraph-format default; explicit requests
for technical detail retain full explanations. The policy demonstrates literal
financial language, requires selecting decisive evidence, and retains unanswered
personal details as unknown instead of guessing or repeatedly asking. A single
illustration demonstrates synthesis; it contains no ticker-specific rule. No
response truncation, phrase replacement, secondary editing model or onboarding
state machine was added.

Real Sol/low checks used isolated fictional profiles:

- `conversation-final.txt`: the same mixed portfolio/Micron question produced
  236 words in three connected paragraphs, versus roughly 580 in the earlier
  failed attempt. It retained reserved cash, indirect ownership and valuation
  uncertainty, without headings, bullet breakdowns, risk-statistic appendices or
  figurative allocation labels. It ended with a relevant funding question.
- `ack-final.txt`: a hesitant "mm" after an unresolved amount question produced
  an 11-word acknowledgment, keeping the amount unknown without another question.
- `depth-final.txt`: an explicit request for technical explanation produced
  1,987 words with equations, estimation choices and limitations.
- `conversation-transfer.txt`: a different Apple/VTI example surfaced indirect
  ownership proactively without declaring the allocation unsuitable. This probe
  preceded the final paragraph-format refinement.

Outputs are in ignored `private/agent-smoke`; no real-client transcript is
committed. These checks establish observed improvements, not a universal style
guarantee or independent verification of every live market claim. Full suite:
355 passed; final agent/MCP checks: 12 passed. Skill validation and build passed.
