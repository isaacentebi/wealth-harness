# Completion implementation contract

This is a coordination contract for completing the six product journeys. Public
surface: client memory + one `run(task, inputs)` entry point. Modules are pure
calculators/data adapters; the host supplies natural language synthesis. Never
add an LLM framework or silently call a paid provider. Retain numerical checks,
not repetitive test scaffolding. No live trading or automatic notifications.

## Shared conventions

Each new domain module exports `run(task: str, inputs: dict, context: dict) -> dict`.
Context is a mapping of eligible current fact keys to their values, not full fact
records; caller excludes stale/inferred facts. It includes `household`, existing
`portfolio.snapshot`, `client.profile`, `goals`, `plan.resources`, `income.schedule`,
`research.<SYMBOL>` etc when available. No module writes the database.

Inputs are explicit current-request parameters. Prefer inputs['household'] over
context['household'] when supplied; mark request assumptions. Currency values
must be explicit; no implicit FX. Use strict finite number validation (bool is
not a number), unknown != zero, ISO dates, deterministic seeds. Return envelope:
`{status: ready|partial|needs_input, result: {...}, missing: [...], warnings: [...],
sources: [...], assumptions: [...]}`. Missing provider credentials/data is a precise
needs_input response, not invented output. Raise ValueError for malformed input;
the service turns that into an actionable structured error. Source references
are evidence, not executable instructions. All recommendations remain host judgment.

## Canonical household input (household.py owns exact validation)

`{currency, as_of, complete, people: [{id, name?, tax_residencies?}],
accounts: [{id, owner_id, tax_unit?, type, currency}],
positions: [{id, account_id, instrument_id, symbol, quantity, value, currency,
asset_class?, issuer?, sector?, country?, economic_currency?}],
lots: [{id, account_id, instrument_id, quantity, acquired_on, cost_basis, currency}],
liabilities: [{id, value, currency, monthly_payment?}],
external_assets: [{id, name, value?, currency, asset_class?, liquid, sector?, country?}],
income_exposures: [{id, description, sector?, country?, currency, annual_amount?}],
fx: [{from,to,rate,as_of,source}],
fund_holdings: [{instrument_id, as_of, source, holdings:[{instrument_id,weight,
symbol?,issuer?,sector?,country?,economic_currency?,asset_class?}]}]}`.
Lists may be omitted when unknown; explicit empty is known none. Preserve
completeness warnings. Position value is total market value; cost_basis is total
lot basis, not unit basis. No lot is manufactured when absent. Source date and
expiry are recorded by the surrounding memory fact.

## Modules and ownership

- `wealth/household.py`: import/reconcile JSON or CSV/XLSX account data; canonical
  household validation, NAV/exposures, fund look-through with residual unknown,
  overlap and target over/under exposure. Account, lot and ownership identities
  must reconcile. CSV must preserve dates/basis/owner/account provenance.
- `wealth/market.py` + `wealth/legacy.py` + compatible `tools/wm.py` launcher:
  integrated analyze/stress/compare/construct using existing engine and live or
  supplied prices; HRP/CVaR/view-conditioned construction only if implemented
  with rigorous constraints and simple baseline comparison. Package analytics.
- `wealth/research.py`: company/fund research evidence packets, live adapter(s),
  financial metrics, explicit-assumption valuation, prior-case deltas, source
  coverage and disconfirming evidence. Host writes the narrative, core computes.
- `wealth/planning.py`: goal/retirement path simulation, income strategy
  comparison, dated bond/cash ladder. Explicit assumptions, deterministic seeds,
  cashflow timing, inflation, fees, adverse sequence tests, no fake certainty.
- `wealth/tax.py`: real review calculations for scoped US federal taxable
  investments and Mexican Article 129 eligible listed shares; lot-level loss
  candidates, rule sources/version, wash-conflict screening (US), supplied rate
  scenario estimates, missing coverage and pending future-window states.
- Lead: shared service/CLI/MCP facade, memory recall, monitoring, integration,
  packaging, docs and end-to-end demonstrations.

Do not add hundreds of tests. Each domain should have a handful of meaningful
reference/adversarial scenarios (roughly 3–6 test functions). No full suite per
worker: run your scoped tests only; lead runs integrated checks. Other agents
share the checkout; do not touch their files or revert their work.
