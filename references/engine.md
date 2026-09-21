# Historical analytics engine reference

This reference covers `tools/wm.py`, which remains separate from the new persistent client core. All commands below run from the repository root. The legacy script declares Python 3.10+; the new package requires Python 3.11+. Live adapters require internet and their provider dependencies. `uv run tools/wm.py ...` resolves the script's dependencies on first use. In an already provisioned environment, `python tools/wm.py ...` also works.

## Small command map

| Task | Command | Boundary |
|---|---|---|
| Price sample | `prices TICKER ...` | Adjusted daily closes, not quotes |
| Portfolio diagnosis | `analyze TICKER ... --weights ...` | Positive long-only weights; normalized, no missing holdings |
| Two allocations | `compare comparison.json` | Same sample and convention |
| Construction | `build TICKER ...` | No expected-return forecast |
| Periods and correlation changes | `regimes TICKER ...` | Descriptive windows, not detected regimes |
| US factor exposures | `factors TICKER ... --model 3` | USD only; complete sample; model 5 also available |
| Dated manager disclosure | `holdings CIK` | Unverified ticker suggestions are not investable weights |
| Statement or position file | `ingest FILE` or `ingest --positions JSON` | Stated values at face value; PDFs are agent-structured |
| Snapshot report | `report portfolio.json` | Saved data unless `--refresh` |
| Capital reservations | `plan profile.json` | Declared assumptions, not suitability approval |
| Portfolio drift | `review review.json` | User-chosen target and band; not orders |
| Isolated fee scenario | `fees --initial N --gross-return R --fees F ... --years Y` | Hypothetical gross return, not forecast |

`--currency` supplies the reporting currency. Live mixed-currency inputs require it; homogeneous native inputs can remain in their native currency. `--years` selects a lookback ending at the latest observation, not necessarily today; inspect actual dates. Annualization uses 252 weekday-close observations. The live adapter explicitly samples seven-day markets at weekday closes. Local data with weekends must be explicitly resampled before 252-day analysis.

`--rebalance annual|daily|none` defaults to annual as a calculation convention. `analyze`, `build`, `compare`, `factors` and `regimes` support `--card file.html`; `--fragment` removes the standalone wrapper. JSON is the authoritative result; `--pretty` is an inspection convenience, not a client-facing report. Do not invent a `--growth` flag.

Outputs require explicit paths. Existing output files require `--overwrite`. `build --save portfolio.json` persists a snapshot only on an explicit user request. This legacy script has no automatic client-memory integration or scheduler. The new `wealth` service provides explicit memory writes; no background scheduler exists. Files and caches are plaintext.

## Offline use

A self-contained hypothetical fee calculation:

```bash
uv run tools/wm.py fees --initial 100000 --gross-return .05 --fees .001 .01 --years 20
```

For historical analysis without a provider call, supply your own adjusted-price
CSV and optional metadata as described below. Those inputs must actually exist;
this checkout does not ship a historical market-data fixture or `tools/demo.py`.
The separate `uv run python examples/returning_client.py` demonstrates persistent
client memory with fictional data and no price calls.

## Local price input

`--price-file` is a CSV with a first date column and one column per asset, including the reference benchmark when requested. Prices must be finite, positive, ordered, unique and complete on the common sample. They must already be in the declared `--currency`. The program does not convert a local CSV or infer distribution adjustments. No silent missing-value filling is performed.

Optional metadata is read from `--metadata-file` or a matching `prices.meta.json`. Fields: `currency`, `source`, `retrieved`, `data_kind`, and `assets`. Each asset may have `currency`, `expense_ratio` (verified annual decimal), `source` and `retrieved`. Local analysis omits risk-free-dependent statistics rather than making a network call; `factors` still requires its factor source.

Live metadata retains raw provider fee fields but does not guess their units. For fee analysis, the caller must supply verified metadata through the Python API or a local metadata file. Source availability and commercial rights are separate questions.

## Ingestion input

`ingest` turns a brokerage export or agent-read positions into holdings.
CSV/XLSX files are parsed as tables: a symbol/ticker column plus a quantity or
value column must exist, and sectioned reports (IBKR-style
`Positions,Header`/`Positions,Data` rows) are supported. Column matching is
name-based; anything it cannot verify is reported, not guessed. PDF and image
input is never parsed: `ingest file.pdf` returns extracted text with
`needs_agent: true`, and the agent re-submits what it reads through
`--positions` JSON (`{"positions": [{"symbol","name","quantity","value",
"currency"}]}` or a bare list; `value` or `quantity` suffices; `-` reads stdin).
`--currency` is required and declares what values are reported in.

Valuation order: stated value, then stated price × quantity, then a live last
close for security rows; money-market rows count at face value. Per-row
currencies are converted at the latest FX close; a missing rate drops the
position from totals with a warning rather than guessing. Cash-like rows,
options and unmapped names are excluded from `analysis_ready` (securities
renormalized to 1) but stay in `positions` and `unresolved` with
`included_in_total` stated. This is normalization, not verification: totals
are exactly what the statement or the agent declared.

## Planning input

Read `examples/planning-input.json`. Required fields: `currency`, `as_of`, `available_capital`, `monthly_essentials`, `reserve_months`, `reserve_outside_pool`, `goals`, `debts`, `debt_payments_from_pool`.

Each goal needs `name`, `currency`, `due`, `protect_now` (boolean), `target_amount` and `funded_outside_pool`. Each debt needs `name`, `currency`, `balance` and `apr` (decimal, not a percentage-point number). Empty lists mean explicitly none. Missing fields mean unknown. Protected foreign-currency goals block a combined remainder until the caller provides a sourced explicit conversion in a revised input. Preserve the original amount, rate and rate date in the agent's notes; do not overwrite its meaning.

Reserve from pool = max(0, essentials × chosen months − reserve outside pool). Protected goals deduct only their unfinanced amount. The remainder then deducts explicitly chosen debt payments. All outside funds must be disjoint. The engine cannot discover double-counted user statements or judge the adequacy of a reserve. Optional narrative fields such as income stability and business exposure can accompany a profile, but the arithmetic engine does not score them.

## Review input

Fields: `values`, `target`, `threshold_pp`, `currency`, `as_of`. Values are current market values, not shares or cost basis. Inventory must be complete and consistently valued. A target holding absent from a complete current inventory has weight zero. Threshold is percentage points; a 5-point band is not a 5% relative change. Drift exactly at the boundary is not flagged; greater absolute drift is flagged. The output contains no order quantities.

## Construction and comparison input

`build --method equal|invvol|minvar|riskparity --max-weight N` enforces the global cap. Default cap 1 means no additional concentration constraint, not a recommended allocation. Optional `--sleeves` and `--sleeve-weights` accept JSON mappings; every candidate belongs to exactly one sleeve, and sleeve shares must identify the same names. For `compare`, use `current_weights` and `proposed_weights` dictionaries, as in the example. Weights can be proportions or positive amounts and are normalized. Shorting and leverage are not modeled.

## Snapshot and data contracts

Saved statistics include source, sample dates, reporting currency and a SHA-256 fingerprint of price values (12 significant digits), dates, labels and reporting currency. A saved report uses its full price snapshot. Changing prices or currency without recomputing statistics is an error. Full report weights and rebalance convention must agree with the supplied statistics. `report --refresh` intentionally replaces the data and recomputes all statistics; it is not monitoring.

## SEC access

Set `SEC_USER_AGENT` to a descriptive application name and a real contact email in the execution environment; do not commit the value. The engine requires this identification before access. It does not provide credentials. Latest amended disclosures require reconciliation outside this limited adapter. Name matching is a suggestion; class and instrument verification is required before any subset backtest. Short positions and unreported assets remain unknown, not zero.

## Errors and limitations

The command exits nonzero with a structured error for handled invalid inputs. Providers can still change schemas or refuse requests. No taxes, trading costs, cash-flow performance accounting, liability valuation, inflation model, retirement simulation, causal regime detection or out-of-sample strategy validation is implemented. Price caches are daily; read dates before calling a result current. The saved CSV/snapshot route is the reproducible calculation path.
