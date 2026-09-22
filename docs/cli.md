# CLI and MCP reference

The JSON CLI, the MCP server and the browser chat share one service
([wealth/service.py](../wealth/service.py)) and one task catalog
([wealth/catalog.py](../wealth/catalog.py)). Every example below runs offline
against a fictional profile `ana`; point `WEALTH_DB` at a scratch file first:

```sh
export WEALTH_DB=/tmp/wealth-demo/wealth.sqlite3 && mkdir -p /tmp/wealth-demo
```

## Programs

| Program | What it runs |
| --- | --- |
| `wealth` | The JSON CLI below, plus text-channel commands |
| `wealth-chat` | The local browser chat and You page ([agent.md](agent.md)) |
| `wealth-agent` | The same assistant in a terminal ([agent.md](agent.md)) |
| `wealth-mcp` | The stdio MCP server for your own agent |

```sh
uv run wealth-chat --client ana          # http://127.0.0.1:8765/
uv run wealth-agent --demo --prompt "Where should I start?"
uv run wealth-mcp                         # speaks MCP on stdin/stdout
```

The two assistants need a signed-in Codex CLI; `wealth` and `wealth-mcp` need
no model at all. `WEALTH_MCP_TOOLS=wealth_context,wealth_run` limits the MCP
server to the named tools. The server's instructions carry the tool rules and
a compact conversation contract (about 2,400 characters); set
`WEALTH_BEHAVIOR_IN_SERVER=1` to append the full policy
([wealth/instructions.md](../wealth/instructions.md), about 21,000 characters)
for a host that has no other copy of it. `WEALTH_BEHAVIOR_IN_HOST=1`, which the
Wealth launcher sets because it gives the model the policy directly, always
leaves the full policy out.

## MCP tools

Nine tools. `wealth_context`, `wealth_recall` and `wealth_inspect` are
read-only; none is destructive. `wealth_run` and `wealth_ingest` are marked
open-world: they may fetch public market, fund and SEC data or read a
connected broker. `wealth_run` is not read-only even without `save_as`,
because some tasks keep state (monitor rules, dismissed nudges, prepared order
tickets). Arguments are strict: unknown fields are rejected, and errors name
the field and the expected inputs.

Discovery is sized for a model's context: `wealth_context` with no arguments
lists every task with its purpose and required inputs; `{"intent": "<task>"}`
returns one task's full schema and a runnable example; `{"detail": "full"}`
returns every schema at once (large).

| Tool | What it does | Example arguments |
| --- | --- | --- |
| `wealth_context` | Task schemas without `client_id`; with it, the facts relevant to a task, or the whole picture with `intent="situation"` | `{"intent": "debt_payoff"}` |
| `wealth_client` | `list` the profiles (ids and names); `create` the profile once; `index` a host-supplied embedding for a fact | `{"action": "create", "client_id": "ana", "inputs": {"display_name": "Ana"}}` |
| `wealth_remember` | Save sourced facts, corrections or `merge` patches atomically; returns a receipt with `needs_user` | `{"client_id": "ana", "facts": [{"key": "spending.monthly", "value": {"total": 30000, "currency": "MXN"}, "source": {"kind": "user", "ref": "conversation", "observed_on": "2026-09-21"}}]}` |
| `wealth_recall` | Search all remembered facts | `{"client_id": "ana", "query": "spending"}` |
| `wealth_run` | Run one task; optional `save_as` with `expires_on` | `{"task": "debt_payoff", "inputs": {"monthly_amount": 3000, "liabilities": [{"id": "card", "balance": 18000, "annual_rate": 0.42, "monthly_payment": 1200, "currency": "MXN"}]}}` |
| `wealth_inspect` | Current facts (`key`/`keys`), one key's `history`, pending `contradictions`, or an `export` | `{"client_id": "ana", "key": "spending.monthly", "detail": "history"}` |
| `wealth_resolve_contradiction` | Save the person's answer (`keep`, `use_new`, `changed`) to a contradiction | `{"client_id": "ana", "contradiction_id": "<id from needs_user>", "choice": "keep"}` |
| `wealth_ingest` | Uploads, extractions, stated balances and connector syncs into a held proposal; `confirm` saves it after the person's yes | `{"client_id": "ana", "action": "connector_status", "inputs": {}}` |
| `wealth_decision` | Propose, accept or dismiss an evidence-bound decision; acceptance is not execution | `{"action": "propose", "client_id": "ana", "inputs": {"title": "Pay the card first", "rationale": "42% costs more than any safe return", "expected_revision": 1, "evidence_ids": ["<fact id>"]}}` |

## Tasks

`wealth_run` (CLI `run`) takes one of these task names. `printf '{}' | uv run
wealth context` prints every task with its inputs and a runnable example.

| Life area | What Wealth does (tasks) |
| --- | --- |
| Money in and out | Where money goes and what is investable, cash calendars, projections, withdrawal and liability matching, debt payoff, the debt engine (amortization, prepay vs invest, refinance offers, payoff strategies), reserves and goals, today's CETES and T-bill rates (`spending`, `calendar`, `income`, `project`, `ladder`, `debt_payoff`, `debt`, `plan`, `reference_rates`) |
| What you own | Holdings, lots, gains and income from a transaction ledger, returns, exposure and overlap, household import, SIC premium (`ledger`, `performance`, `exposure`, `import`, `sic_premium`) |
| Investing | Investment policy, tax-aware rebalancing, asset location, recurring investing, portfolio analysis and construction, company and fund research (`policy_draft`, `policy_check`, `rebalance`, `asset_location`, `dca`, `compare`, `construct`, `analyze`, `factors`, `stress`, `research`, `value`) |
| Tax | US federal lots, wash sales and harvesting; Mexico Art. 129, real interest, deductions and PPR, foreign securities, tax calendar; US estate exposure for non-residents; the annual tax pack for your contador or CPA (`tax`, `mx_holdings`, `mx_interest`, `mx_deductions`, `mx_foreign`, `mx_calendar`, `estate`, `tax_pack`) |
| Retirement | IMSS Ley 73/97, AFORE and Modalidad 40; Social Security, contribution limits and withdrawal order; a readiness range (`retirement_mx`, `retirement_us`, `retirement_readiness`) |
| Protection | Insurance and estate gaps, an estate and beneficiary register (who receives each account at death, by which mechanism, and the gaps ranked by amount at risk), life events, and guardrails for speculation, panic selling and scams (`protection_review`, `estate_register`, `life_event`, `speculation_check`, `panic_check`, `scam_check`) |
| Reviews and nudges | What needs attention today, a weekly letter, a quarterly review, a fee audit, opt-in monitor rules (`today`, `weekly`, `quarterly_review`, `fee_audit`, `monitor`) |
| Following managers | Find a fund manager's SEC 13F filings, read and profile them, compare managers, size a mirror within your policy (`manager_search`, `manager_holdings`, `manager_profile`, `manager_compare`, `manager_mirror`) |
| Connections | Statement uploads, plus read-only syncs from Interactive Brokers, Alpaca and Cuenca, each saved only after you say yes (`wealth_ingest`: `ibkr_flex`, `alpaca`, `cuenca`) |
| Execution | An order ticket with pre-trade checks for Alpaca that you place yourself by tapping its card (`order_ticket`) |

## CLI commands

Every command reads one JSON object from stdin or `--input request.json` and
prints JSON; `--db` overrides `WEALTH_DB`. Errors go to stderr as
`{"error", "error_type"}` with exit code 2. Operations that save on the
person's yes (`ingest` confirm/confirm_duplicates, `decision` accept,
`resolve_contradiction`) need an interactive terminal or `--yes`, passed only
after the person has agreed; piped stdin alone is refused.

| Command | MCP equivalent | What it does |
| --- | --- | --- |
| `context` | `wealth_context` | Task schemas or relevant facts |
| `client` | `wealth_client`, `wealth_inspect` | `list`, `create`, `inspect`, `export`, `index`; `forget` in a terminal only |
| `remember` | `wealth_remember` | Save facts |
| `recall` | `wealth_recall` | Search facts |
| `run` | `wealth_run` | Run a task |
| `decision` | `wealth_decision` | Propose, accept, dismiss; accept needs a terminal or `--yes` |
| `ingest` | `wealth_ingest` | Proposals and confirmation; confirm needs a terminal or `--yes` |
| `history` | `wealth_inspect` `detail=history` | One key's timeline |
| `contradictions` | `wealth_inspect` `detail=contradictions` | Pending contradictions |
| `resolve_contradiction` | `wealth_resolve_contradiction` | The person's answer; needs a terminal or `--yes` |
| `execution_status` | none | Trading mode, whether keys exist, limits, today's usage |
| `order_status` | none | Order tickets and line states; `refresh` reads the broker |
| `prices` | none | Market-data cache: `status`, or `refresh` now |
| `rates` | `wealth_run` `task=reference_rates` | CETES and T-bill reference rates: `status`, or `refresh` now |
| `forget` | none | Delete a profile; interactive terminal only |
| `watch` | none | Foreground monitor polling |
| `tax-pack` | `wealth_run` `task=tax_pack` | Write the annual tax pack: JSON, one CSV per section, printable HTML |
| `onboarding`, `today`, `view` | none | Text-channel helpers ([openclaw.md](openclaw.md)) |

**context**, with no client, returns the catalog; name one task for its inputs
and example:

```sh
printf '{"intent":"debt_payoff"}' | uv run wealth context
```

**client** creates the profile (then `inspect`, `export`, `index`, below):

```sh
printf '%s' '{"action":"create","client_id":"ana","inputs":{"display_name":"Ana"}}' | uv run wealth client
```

**remember** records sourced facts. A new key needs no revision:

```sh
printf '%s' '{"client_id":"ana","facts":[
  {"key":"income.salary","value":{"amount":42000,"currency":"MXN","frequency":"monthly","net":true,"kind":"salary"},
   "source":{"kind":"user","ref":"conversation","observed_on":"2026-09-21"}},
  {"key":"liability.card","value":{"kind":"card","balance":18000,"annual_rate":0.42,"payment":1200,"payment_frequency":"monthly","currency":"MXN"},
   "source":{"kind":"user","ref":"conversation","observed_on":"2026-09-21"}}]}' | uv run wealth remember
```

**recall** searches what is remembered:

```sh
printf '%s' '{"client_id":"ana","query":"salary"}' | uv run wealth recall
```

**run** runs one task. With `client_id` it reads the saved picture and ledger;
direct inputs override them for that call. `save_as` (`analysis.<name>`,
`research.<symbol>`, or `household` for `import`) also needs `expires_on`.

```sh
printf '%s' '{"task":"debt_payoff","client_id":"ana","inputs":{"monthly_amount":3000}}' | uv run wealth run
```

**Risk tasks.** `speculation_check` reads a `sell` of an option as writing it
(sell to open) unless `position_effect` is `close`, and sizes it by the capital
it puts at risk: an uncovered put risks strike x 100 x contracts less the
premium, an uncovered call has no ceiling. With `proposal.legs` (calls, puts,
stock, crypto, leveraged), or strike, contracts and premium, `result.payoff`
gives P&L at expiry over a price grid, max loss and max gain (`null` with
`*_unbounded`), breakevens, capital at risk and its share of net worth and of
the play-money budget, with an en/es explanation; the chat draws it as two
tickets. `action` `explain` returns the payoff without a verdict:

```sh
printf '%s' '{"task":"speculation_check","client_id":"ana","inputs":{"proposal":{"action":"explain",
  "instrument":"options","symbol":"SPY","spot":650,"legs":[
  {"type":"call","side":"long","strike":650,"premium":12,"contracts":1},
  {"type":"call","side":"short","strike":670,"premium":5,"contracts":1}]}}}' | uv run wealth run
```

`stress` accepts partial shocks: holdings without one move by their beta to the
scenario's `factor` (default: the first shocked symbol), estimated from price
history (supplied `prices`/`beta_prices`, or the price cache), else from
explicit `betas`, else from a stated asset-class default when the factor is an
equity index. `fx_shocks` (`{"USDMXN": 0.15}` = 15% more pesos per dollar)
convert each holding's own-currency return into the reporting currency; the
holding's currency comes from `positions[].native_currency` or
`asset_currencies`, and stays unknown otherwise. A return nobody can work out
is `null` with a reason, and the scenario total with it.

```sh
printf '%s' '{"task":"stress","client_id":"ana","inputs":{"scenarios":[
  {"name":"US selloff, peso weaker","shocks":{"SPY":-0.25},"fx_shocks":{"USDMXN":0.15}}]}}' | uv run wealth run
```

`analyze` adds `result.tail_risk`: historical 1-day and 1-month 95% VaR and
CVaR of today's weights (at least 250 and 500 daily returns), parametric
normal VaR/CVaR as the fallback, and each position's contribution (historical
CVaR and Euler parametric VaR, both adding up to the total).

**client inspect / export** read facts, and **decision** cites them. A proposal
needs the current revision and eligible evidence:

```sh
FACT=$(printf '%s' '{"action":"inspect","client_id":"ana","inputs":{"key":"liability.card"}}' \
  | uv run wealth client | python3 -c 'import json,sys; print(json.load(sys.stdin)["facts"][0]["id"])')
REV=$(printf '%s' '{"action":"inspect","client_id":"ana"}' \
  | uv run wealth client | python3 -c 'import json,sys; print(json.load(sys.stdin)["client"]["revision"])')
printf '%s' "{\"action\":\"propose\",\"client_id\":\"ana\",\"inputs\":{\"title\":\"Pay the card first\",
  \"rationale\":\"42% costs more than any safe return\",\"expected_revision\":$REV,\"evidence_ids\":[\"$FACT\"]}}" \
  | uv run wealth decision
printf '%s' '{"action":"export","client_id":"ana"}' | uv run wealth client > /tmp/wealth-demo/ana-export.json
```

Accept or dismiss later, after the person agreed, with `--yes`:

```sh
printf '%s' '{"action":"accept","client_id":"ana","inputs":{"decision_id":"..."}}' \
  | uv run wealth decision --yes
```

**ingest** turns a statement or stated balances into a held proposal; confirm
only after the person's yes, with `--yes`:

```sh
PID=$(printf '%s' '{"client_id":"ana","action":"chat","inputs":{"currency":"MXN","as_of":"2026-09-21",
  "items":[{"kind":"cash","label":"Nu","amount":40000,"quote":"tengo unos 40 mil en Nu"}]}}' \
  | uv run wealth ingest | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["proposal_id"])')
printf '%s' "{\"client_id\":\"ana\",\"action\":\"confirm\",\"inputs\":{\"proposal_id\":\"$PID\"}}" | uv run wealth ingest --yes
```

**history** prints one key's timeline in words and entries:

```sh
printf '%s' '{"client_id":"ana","key":"income.salary"}' | uv run wealth history
```

**contradictions** and **resolve_contradiction**: evidence that disagrees with
what the person said is held, not saved, until they answer. A figure from a web
page (here the card's published rate) opens a contradiction; a statement,
payslip or connected account instead settles the figures it covers:

```sh
printf '%s' '{"client_id":"ana","facts":[{"key":"liability.card","merge":true,"value":{"annual_rate":0.45},
  "source":{"kind":"web","ref":"https://www.example.com/tarjetas/tasas","observed_on":"2026-09-21"}}]}' | uv run wealth remember
CID=$(printf '%s' '{"client_id":"ana"}' | uv run wealth contradictions \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["contradictions"][0]["id"])')
printf '%s' "{\"client_id\":\"ana\",\"contradiction_id\":\"$CID\",\"choice\":\"keep\"}" | uv run wealth resolve_contradiction --yes
```

**client index** attaches a host-computed embedding to a fact (Wealth creates
none):

```sh
printf '%s' "{\"action\":\"index\",\"client_id\":\"ana\",\"inputs\":{\"fact_id\":\"$FACT\",\"embedding\":[0.1,0.2,0.3],\"model\":\"example-embed-3\"}}" \
  | uv run wealth client
```

**run order_ticket, execution_status, order_status**: a ticket stores exact
orders and their pre-trade checks. Without Alpaca keys it is `partial`, with the
checks it could not run marked `unknown`. Nothing here can place an order; only
the person's tap on the card in `wealth-chat` does ([trading.md](trading.md)).

```sh
printf '%s' '{"task":"order_ticket","client_id":"ana","inputs":{"source":"user_request",
  "rationale":"Invest USD 500 in the total-market fund.","orders":[{"symbol":"VTI","side":"buy","notional":500}]}}' \
  | uv run wealth run
printf '%s' '{"client_id":"ana"}' | uv run wealth execution_status
printf '%s' '{"client_id":"ana"}' | uv run wealth order_status
```

**prices** manages the market-data cache ([wealth/prices.py](../wealth/prices.py)).
`status` lists each cached symbol with its last date and age; `refresh` fetches
the latest closes now, for `symbols` or for what a client's ledger holds plus
the FX into the reporting currency. Prices come from Yahoo Finance through
yfinance and are cached in the Wealth database: 15 minutes for a latest price
while markets are open (weekdays 13:30-21:00 UTC), 12 hours otherwise; a past
day fetched after it closed is final. Price lookups happen per turn, for the
symbols a client currently holds. `WEALTH_OFFLINE=1`, or a failed fetch,
serves the cache labelled `cache_fallback`, or leaves the price missing; a
price is never zero or guessed. A price more than five days older than the
date it values is listed in `stale_prices`.

```sh
printf '%s' '{"action":"status"}' | uv run wealth prices
printf '%s' '{"action":"refresh","client_id":"ana"}' | uv run wealth prices
printf '%s' '{"action":"refresh","symbols":["AAPL *","NAFTRAC ISHRS",{"symbol":"VOO","venue":"us"}]}' | uv run wealth prices
```

Symbols map to Yahoo: SIC and BMV listings take `.MX` with the series appended
(`AAPL *` -> `AAPL.MX`, `WALMEX *` -> `WALMEX.MX`, `NAFTRAC ISHRS` ->
`NAFTRAC.MX`, `GFNORTE O` -> `GFNORTEO.MX`); US share classes take a dash
(`BRK.B` -> `BRK-B`); FX is `USDMXN=X`. CETES, UDIBONOS and other Mexican
government bonds are not on Yahoo: they use a statement or trade price
(accrued at a stated rate when one is known) or stay missing.

With a provider in place, the picture values ledger-only accounts (every
holding priced and the cash anchored by an opening or statement balance), the
You page overview and `today`/`weekly` see them, `rebalance` and `dca`
backtests get prices when none are supplied, and `quarterly_review` gets
period prices, FX and an IPS benchmark built from labelled proxies: global
equity is the ACWI ETF (total return, converted from USD at the daily Yahoo
rate), US bonds AGG, USD cash BIL, the 60/40 reference ACWI with BNDW, and
CETES accrue at the published 28-day rate only when one is supplied (otherwise
that benchmark is missing). Each derived number names its price source and
date in `sources` or `market_data`.

**rates** manages the reference-rate cache ([wealth/rates.py](../wealth/rates.py)),
the one source of the CETES and T-bill rates that price idle cash (`today`),
the risk-free side of `debt` `prepay_vs_invest` and the fee audit's cash drag.
The 13-week, 26-week and 52-week bills come from Treasury Fiscal Data's
auction results (high investment rate, the bond-equivalent yield, of the latest
auction), with TreasuryDirect as the backup. CETES 28 days comes from the
primary-auction yield Banco de México publishes on its home page, no token
needed. With a free Banxico SIE token in `BANXICO_TOKEN` or the keychain
(service `wealth-banxico`, read only) the 91, 182 and 364-day CETES come from
SIE series SF43939, SF43942 and SF43945 (SF43936 backs up the 28-day rate).
Rates are cached in the Wealth database and fetched at most once a day, in the
background: a turn never waits for the network. The order is a saved
`cash_reference_rate` in that currency, then the latest fetched auction, then
Wealth's built-in dated value. Each rate carries `as_of` (the auction date),
`source`, `origin` (`saved_fact`, `fetched` or `builtin`) and `stale` (more
than 30 days old). `WEALTH_OFFLINE=1` never fetches. `status` reads the cache;
`refresh` fetches now (`currency` MXN or USD, default both).

```sh
printf '%s' '{"action":"status"}' | uv run wealth rates
printf '%s' '{"action":"refresh","currency":"MXN"}' | uv run wealth rates
security add-generic-password -U -s wealth-banxico -a "$USER" -w   # optional SIE token; prompts for it
```

**watch** evaluates the client's opt-in monitor rules and prints only changed
events; it runs in the foreground until stopped (`--interval` seconds, default
300), sends no notifications and places no trades:

```sh
uv run wealth watch --client ana --once
```

**tax-pack** runs `tax_pack` for a client (or `--input` inputs with facts and
a ledger) and writes `tax-pack-<year>.json`, one `tax-pack-<year>-<section>.csv`
per section (plus pendientes, deadlines and reconciliation) and a printable,
bilingual `tax-pack-<year>.html`; print the page to PDF from the browser:

```sh
uv run wealth tax-pack --client ana --year 2025 --lang es --out /tmp/wealth-demo/tax-pack
```

**onboarding, today, view** print person-facing text for text channels. Exit
code 3 means the reply needs the host model, 4 means there is nothing to send.

```sh
uv run wealth onboarding next --client ana --lang en
uv run wealth today --client ana --lang en
uv run wealth view --client ana --task situation --svg /tmp/wealth-demo/picture.svg
```

**forget** deletes one profile and its uploads. It asks you to type the client
ID followed by `DELETE` and refuses piped or scripted input, so an agent with
shell access cannot delete a profile:

```sh
uv run wealth forget --client ana
```

## Action envelopes

| Operation | Action | `inputs` |
| --- | --- | --- |
| `decision` | `propose` | `title`, `rationale`, `expected_revision`, `evidence_ids`, optional `alternatives` |
| `decision` | `accept` / `dismiss` | `decision_id`, optional `expected_revision` |
| `ingest` | `file` | `path` (name or path inside the upload directory), optional `owner_id`, `currency`, `as_of`, `preset`, `aliases`, `tolerance`, `source_text` |
| `ingest` | `extraction` | `extraction_id`, `payload` (the filled `extraction_request.schema`) |
| `ingest` | `chat` | `items` (each with `quote`, the person's words), optional `as_of`, `currency` |
| `ingest` | `confirm` | `proposal_id`, optional `acknowledge_discrepancies`, `expires_on`; only after the person's yes |
| `ingest` | `confirm_duplicates` | `proposal_id`, `entry_ids` of held lines the person says are separate |
| `ingest` | `diff` | `proposal_id`, optional `previous_proposal_id` |
| `ingest` | `connector` | `name` (`ibkr_flex`, `alpaca`, `cuenca`); `query_id` for `ibkr_flex`; optional `owner_id`, `sic_listed` (`ibkr_flex`, `alpaca`), `paper` (`alpaca`), `since` (`alpaca`, `cuenca`); the credential is never an input |
| `ingest` | `connector_status` | optional `name`; whether a credential is available and the last sync (never the credential) |
| `client` | `create` | `display_name` |
| `client` | `inspect` | optional `detail: current|history|contradictions|export`; `key` or `keys` filters current facts; `key` is required for history |
| `client` | `export` | none; full private history including superseded facts, decisions and the order audit |
| `client` | `index` | `fact_id`, host-supplied `embedding`, exact `model` identifier |
| `client` | `forget` | `confirm_client_id` matching the requested client (interactive terminal only) |
| `order_status` | none | `client_id`, optional `ticket_id`, `refresh` |

## Ingestion

`ingest` reads files only from the client's upload directory:
`WEALTH_UPLOAD_DIR/<client>` when set, otherwise `<db dir>/uploads/<client>`, the
directory the browser chat saves attachments to. Proposals are held server-side
by `proposal_id`; `confirm` saves the held proposal (never one sent by the
caller) as `account.<id>`, `liability.<id>` and `income.<id>` facts (a tax document: see below) and posts
its accounts, holdings, transactions, balance checks and FX to the ledger
(batch `ingest:<proposal_id>`). An account new to the ledger gets opening
balances derived from the statement; an account already there gets only new
lines, and its closing figures become balance checks. Lines resembling another
statement's are held until `confirm_duplicates`. The confirm result includes
`statement_prices` for `run` task `ledger` view `household`.

`confirm` is one transaction: the facts, the ledger lines and the proposal's
state commit together or not at all, so a failed confirm can simply be retried,
and a repeated or concurrent confirm of the same proposal returns the first
receipt with `replayed: true`. Writes made meanwhile never conflict with it.

A newer statement for an account already in the ledger is reconciled to it:
after its own lines are posted, any cash or position the lines do not explain
gets a labelled `Statement reconciliation` adjustment dated at the statement
(cash as an opening-balance correction, extra units as an opening position
without basis, missing units as an outgoing transfer, so no gain is invented).
The receipt lists them in `result.ledger.adjustments`, and holdings the
statement no longer lists in `result.reconciliation.missing_positions` (also
shown on the proposal before confirming). An older statement only adds balance
checks. Forgetting an `account.<id>` fact posts reversal entries for that
account's ledger lines (append-only; the history stays), so the account leaves
the brief, the net worth and `task=ledger` together.

**Annual tax documents.** `ingest action=file` recognises, from the title of
the first page, a Mexican constancia fiscal anual (brokers such as GBM,
Actinver, Banorte and Kuspit/Cetesdirecto: Art. 129 gain/loss/net, interest
nominal/real/real loss, ISR withheld, dividends; banks such as BBVA, Banorte,
Santander and Nu: interest), a US Form 1099 composite (1099-B lots and totals,
1099-DIV, 1099-INT; Schwab, Fidelity, Vanguard, Alpaca) and Form 5498. Regular
layouts (label or box lines, the 1099-B lot table) are read deterministically;
anything else returns an `extraction_request` with the tax-document schema
(`document_kind: "tax_document"`), validated by `action=extraction` as usual.
Every figure must appear in the page text and the totals must reconcile (1099-B
lots against the printed term and overall totals, gain - loss = net, nominal -
inflation adjustment = real interest); a document that does not is
`needs_review` and needs `acknowledge_discrepancies`. The proposal shows
`figures`, `lots`, the checks and `facts_preview`; `confirm` saves exactly
those as `constancia.<institution>_<year>[_1099|_5498]_<account>` facts with
`source.kind=document` citing the file, and posts nothing to the ledger.
`<account>` is the last four digits of the account the document prints, else
`h` and a short hash of the document (type, institution, year, issue date,
figures): two accounts' documents from one institution never replace each
other, and uploading the same document again rewrites the same key. A 1099's
`form_1099_b` keeps its lots (description, symbol, quantity, acquired, sold,
proceeds, basis, wash sale, gain, term, box; up to 5,000).
`task=tax_pack` then declares the document's figures and shows its own
computation next to them: several documents of one institution are summed per
block (each one's figure listed under `documents` in the reconciliation), a
1099-B's lots are the Form 8949 rows for the accounts it covers (the ledger's
sales there are reconciled against them, never added), and an uploaded
document is preferred over a typed one for the same institution.

**Upload retention.** Raw statements hold RFC, CURP, CLABE and account numbers,
so uploads are deleted (overwritten, then unlinked) once they are no longer
needed: the file of a confirmed statement right after `confirm`, any other
upload 30 days after it was saved (checked on every `ingest` call), and the
client's whole upload directory when the client is deleted. Set
`WEALTH_UPLOAD_RETENTION_DAYS` to change the 30 days (`0` turns age-based
deletion off) and `WEALTH_KEEP_CONFIRMED_UPLOADS=1` to keep confirmed files.

Amounts, balances and quantities must be below 10^15 in absolute value and rates
within their documented range; a fact saved before these bounds that no reader
can use is left out of the picture and listed in `invalid_facts` (situation and
profile) so the person can remove it.

## Connectors

`ingest action=connector` pulls a read-only account connector on demand and
returns the same held proposal as an upload: show the summary, then `confirm`
only after the person says yes. Nothing runs in the background.

**Interactive Brokers (`ibkr_flex`)** uses the IBKR Flex Web Service
(`wealth/connectors/ibkr_flex.py` cites IBKR's documentation). It can only read
reports; it cannot trade or move money.

1. In the IBKR portal open *Performance & Reports > Flex Queries* and create an
   **Activity Flex Query**: format **XML**, period **Last 365 Calendar Days**,
   date format **yyyyMMdd**, all accounts. Add these sections, each with all
   fields selected:
   - Account Information (Account ID, Currency, Account Type)
   - Net Asset Value (NAV) in Base: the equity summary by report date (Report
     Date, Cash, Stock, Interest/Dividend Accruals, Total)
   - Cash Report (Currency, Ending Cash)
   - Open Positions, level of detail **Summary and Lot** (Symbol, ISIN,
     Listing Exchange, Underlying Symbol, Asset Class, Currency, Quantity, Mark
     Price, Position Value, Cost Basis Money, Multiplier, Open Date Time)
   - Trades, level of detail **Executions and Closed Lots** (Trade ID,
     Transaction ID, Trade Date, Settle Date, Buy/Sell, Quantity, Trade Price,
     Proceeds, Taxes, IB Commission and its currency, Net Cash)
   - Cash Transactions, level of detail **Detail** (Type, Amount, Currency,
     Date/Time, Settle Date, Symbol, ISIN, Transaction ID)
   - Corporate Actions (Type, Description, Quantity, Proceeds, Date/Time,
     Transaction ID, Action ID)
   - Conversion Rates (Report Date, From/To Currency, Rate)

   Note the query id shown next to the saved query. It is not secret.
2. Open *Flex Web Service Configuration*, enable the service and copy the
   current token. Choose an expiry; a new token invalidates the old one, and an
   optional IP restriction limits where it works.
3. Store the token yourself; never paste it into the chat:

   ```sh
   security add-generic-password -U -s wealth-ibkr-flex -a "$USER" -w
   ```

   macOS prompts for the token. On Linux use
   `secret-tool store --label="Wealth IBKR Flex" service wealth-ibkr-flex`, or
   export `WEALTH_IBKR_FLEX_TOKEN` for one session. Wealth reads it only from
   there, and it never reaches SQLite, logs, errors or outputs.
4. `{"client_id": "ana", "action": "connector_status", "inputs": {}}` shows
   whether a token is found. Sync with
   `{"client_id": "ana", "action": "connector", "inputs": {"name": "ibkr_flex", "query_id": "987654"}}`.

A sync calls SendRequest, then polls GetStatement with backoff while IBKR
answers 1019 (statement generation in progress) or another "try again shortly"
code. After 1018 (too many requests; the limit is 1 request per second and 10
per minute per token) it waits at least 10 seconds, and it gives up after 120
seconds. The proposal reconciles positions plus cash per currency (converted
with IBKR's conversion rates) against NAV excluding accruals. Instruments carry
venue, listing exchange, ISIN, underlying symbol and issuer domicile (from the
ISIN prefix: `US`, `IE`, `MX` or `other`). IBKR does not report Mexican SIC
listing, so `sic_listed` stays `unknown` unless you pass it. Transactions carry
IBKR trade and transaction ids, so a re-sync posts only new lines, and
`result.changes` lists what changed since the last confirmed sync. Splits
post with their ratio. Other corporate actions, derivative trades and
unmapped cash types are listed under `ledger.not_posted` with the reason.

**Alpaca (`alpaca`)** uses the Alpaca Trading API v2
(`wealth/connectors/alpaca.py` cites Alpaca's documentation). It sends only
`GET` requests to `/v2/account`, `/v2/positions`, `/v2/orders` (open orders,
listed as a warning), `/v2/account/activities`, `/v2/account/portfolio/history`
and `/v2/assets/{symbol}`; any other method or path is refused before it is
sent, so it cannot place or cancel orders, close positions or move money.

1. In the Alpaca dashboard generate an API key for the live or the paper
   account and copy the key id and the secret (the secret is shown once).
   Paper and live accounts have different keys and hosts.
2. Store both yourself; never paste them into the chat:

   ```sh
   security add-generic-password -U -s wealth-alpaca -a key_id -w
   security add-generic-password -U -s wealth-alpaca -a secret -w
   ```

   On Linux use `secret-tool store --label="Wealth Alpaca" service wealth-alpaca account key_id`
   (and `account secret`), or export `WEALTH_ALPACA_KEY_ID` and
   `WEALTH_ALPACA_SECRET` for one session. For the paper account export
   `WEALTH_ALPACA_PAPER=1` or pass `"paper": true`.
3. Sync with
   `{"client_id": "ana", "action": "connector", "inputs": {"name": "alpaca", "since": "2026-01-01"}}`
   (`since` defaults to 365 days ago).

Activities are paged 100 at a time with `page_token`. Requests are spaced at
least 0.3 s apart (Alpaca's limit is 200 requests per minute per account); an
HTTP 429 or 5xx is retried with backoff that honours `Retry-After`. The
proposal reconciles positions plus cash against account equity; the account is
read before and after the positions, and if equity moved (market open) the
tolerance widens by that amount with a warning. Alpaca reports only an average
entry price, so holdings carry average cost with `lots: "unavailable"`.
Fractional quantities are kept exactly. Fills post as buys and sells at
quantity x price (Alpaca is commission-free; regulatory fees arrive as FEE
lines). DIV and capital-gain distributions post as dividends; DIVNRA, DIVFT,
DIVTW, INTNRA and INTTW as `tax_withheld` on the holding, which keeps the US
withholding a Mexican resident credits in their return. CSD/CSW post as
deposits and withdrawals, JNLC as transfers, INT as interest (negative INT, i.e.
margin interest, as a fee) and FEE/PTC as fees. Splits, mergers, symbol
changes, stock journals, return of capital and option events are listed under
`ledger.not_posted`. Instruments carry venue `us`, listing exchange and CUSIP;
Alpaca has no ISIN, so issuer domicile is set only for CINS CUSIPs (non-US
issuers) and `sic_listed` stays `unknown` unless you pass it. Portfolio history
gives the opening equity of the period for a NAV balance check.

**Cuenca (`cuenca`)** uses the API behind the official `cuenca` Python SDK
(`wealth/connectors/cuenca.py` cites the SDK sources). It sends only `GET`
requests to `balance_entries`, `deposits`, `transfers`, `card_transactions`,
`commissions`, `bill_payments`, `savings` and `statements`, with HTTP Basic
auth as the SDK does. Transfers, card changes, wallet movements, API-key and
login calls are refused before they are sent.

1. Cuenca must have issued you an API key and secret. Cuenca offers its API to
   platforms and does not publish whether an individual app user can get one;
   without a key, upload the monthly *estado de cuenta* instead.
2. Store both yourself:

   ```sh
   security add-generic-password -U -s wealth-cuenca -a api_key -w
   security add-generic-password -U -s wealth-cuenca -a api_secret -w
   ```

   Or export `WEALTH_CUENCA_API_KEY` and `WEALTH_CUENCA_API_SECRET` for one
   session (`WEALTH_CUENCA_SANDBOX=1` selects the sandbox host).
3. Sync with `{"client_id": "ana", "action": "connector", "inputs": {"name": "cuenca"}}`.

Balance entries are Cuenca's own ledger, each with its rolling balance; the
connector checks that the rolling balances chain and that opening plus the
period's movements equals the current balance, then records the balance as a
check on the account. Each *apartado* is its own savings account. Amounts are
MXN (Cuenca amounts are centavos). SPEI movements use their *clave de rastreo*
as the external id, everything else Cuenca's id, so a re-sync posts only new
lines. Descriptions keep Cuenca's Spanish text with CLABEs masked to the last
four digits. Card purchases post as expenses with the merchant as the
description, so spending categories apply as for any statement (refunds offset
spending); ATM withdrawals post as withdrawals, commissions as fees (counted as
bank fees), and incoming SPEI as income only when its text says nómina, sueldo
or honorarios, otherwise as a transfer. Cuenca publishes no rate limit; requests
are spaced 0.25 s apart and 429/5xx are retried with backoff.

**Vest** has no API for individual users (its app offers monthly statements,
trade confirmations and the 1042-S or 1099 tax report, with no documented
export format), so there is no connector and nothing is scraped. Upload the
statement PDF, or a CSV with holdings (Symbol, Description, Quantity, Price,
Market Value, Cost Basis) and/or activity (Date, Type, Description, Symbol,
Quantity, Price, Amount, Fees) using `"preset": "vest"`. The preset is a
generic US-broker layout (USD, month-first dates) and says so in the proposal's
assumptions.

## Debt engine

`debt` takes a `mode` and the client's stored `liability.<id>` facts (or
inline `liabilities`, or one `debt`). Interest accrues monthly at the tasa
(`annual_rate / 12`). Mexican card and consumer credit adds 16% IVA on
interest; home credit does not. Anything unknown goes to `missing` and is shown
as a range, never as zero. Every mode returns views: a balance series, a ticket
or a side-by-side.

- **`amortize`**: months, payoff date, interest (and IVA), total paid, and the
  schedule. The first `monthly_rows` months are listed (default 12, or `"all"`)
  and every year is summarised. A card on `minimum_payment` (Banxico: 1.5% of
  the balance plus interest and IVA, floor 1.25% of the limit) also reports what
  paying only the minimum costs. `cat` is compared with the tasa: CAT includes
  fees and excludes IVA. A card with only a CAT accrues at its monthly
  equivalent, marked as an estimate. Infonavit/Fovissste credits in `VSM` or
  `UMA` run in units. The unit rises each `update_month` (UMA defaults to INEGI
  2026 x 30.4; growth defaults to 4%). A balance left after 30 years is
  reported as cancelled. The whole result is an estimate unless the statement
  gives pesos.
- **`prepay_vs_invest`**: `extra_monthly` and/or `lump_sum` against the debt or
  invested. The debt's after-tax rate is guaranteed. US mortgage interest counts
  only if `itemizes` (up to $750,000 of acquisition debt). The Mexican mortgage
  deduction reuses `mx_deductions`' LISR Art. 151 fr. IV real-interest rule
  (`mx_mortgage`). Investing uses `expected_return {conservative, base}` after
  tax (Art. 129 10% in Mexico, `capital_gains_rate` in the US) and the risk-free
  rate after tax. The risk-free rate is CETES 28 days for MXN and the 13-week
  T-bill for USD: `risk_free`, else a saved `cash_reference_rate`, else the
  latest auction from `rates` (with its `as_of`, `source`, `origin` and
  `stale`). The result gives the
  `breakeven` return, the net-worth difference at `horizon_months` per
  scenario, and a `verdict` (`prepay`, `close_call`, `invest`, `depends`,
  `build_reserve_first`) with a `confidence`. A debt at 20% or more (not a
  mortgage) is paid first once the reserve holds one month of essentials, and
  the reserve keeps filling in parallel (`reserve.in_parallel`). Below that
  month it waits. Lower-rate debt, such as a mortgage or a car loan, is never
  prepaid while the reserve is below its full target.
- **`refinance`**: an `offer` (`refinance`, `balance_transfer` or
  `consolidation`; rate, fees, term, promo) against the current path. It gives
  `interest_saved` net of fees, the `breakeven_month` when fees are paid back,
  and `risk`: the balance left when the promo ends, the payment that clears it
  in time, interest after the promo, and deferred interest.
- **`strategies`**: avalanche, snowball and hybrid (quick wins, then highest
  rate) for `monthly_amount`. It gives months, interest, extra interest against
  avalanche in money, the first debt cleared, and the total balance over time
  for each.

```sh
printf '%s' '{"task":"debt","inputs":{"mode":"prepay_vs_invest","debt":{"id":"mortgage","kind":"mortgage",
  "balance":350000,"annual_rate":0.065,"monthly_payment":2400,"currency":"USD"},"extra_monthly":500,
  "marginal_rate":0.24,"itemizes":false,"investment":"VTI","risk_free":{"rate":0.04,"source":"3-month T-bill"},
  "reserve":{"months":6,"target_months":6}}}' | uv run wealth run
```

The proactive `high_interest_debt` item (20% a year or more) also says the
interest saved for each month sooner the debt is gone. When the payment does
not cover the monthly interest (and IVA), the balance grows: the item is always
shown, as urgent, with `payment_to_stop_growth`. Otherwise it waits only while
the reserve is below one month of essentials. The reserve item then adds
"Después de juntar un mes de reserva, esta tarjeta es tu mejor inversión".
Advice to prepay lower-rate debt still waits for the full reserve target.

## Harvest tickets, idle cash, look-through and Roth conversions

**Harvest to ticket.** `tax` with `mode: harvest_report` returns
`order_tickets`, one per account: `inputs` go to `order_ticket` unchanged.
Each sell line lists `lots` (`lot_id`, `quantity`, `estimated_tax_saving`,
`repurchase_not_before`). A lot's saving is its marginal current-year federal
reduction in plan order. It is `null` when return facts are missing. Lots a wash
sale would disallow, or that only add to carryforwards, are listed in
`order_ticket_exclusions`. The ticket is a proposal. The person confirms it on
the card and asks the broker for specific-lot relief.

```sh
printf '%s' '{"task":"order_ticket","client_id":"ana","inputs":{"source":"user_request",
  "rationale":"Tax-loss harvest ...","orders":[{"symbol":"VTI","side":"sell","qty":100,
  "lots":[{"lot_id":"vti-1","quantity":100,"estimated_tax_saving":"660.00","repurchase_not_before":"2026-04-20"}]}]}}' \
  | uv run wealth run
```

**Idle cash.** `today` adds an `idle_yield` item: cash above the reserve
target, earmarked goal cash and `protect_now` goals, priced at a reference
rate. The rate is a saved `cash_reference_rate` fact
(`{low, high, unit, source, as_of?, currency?}`). Without one, Mexico
residents get the latest CETES 28-day auction rate and US filers' USD cash the
13-week T-bill, from `rates` (or Wealth's built-in dated value until a fetch
has succeeded; `data.reference_origin` says which). The item names the rate's
date and says when it is more than 30 days old. A saved `cash_yield` (what the
cash earns) makes the loss exact. Without it the figure is `bound: at_most`.
`data.offer` holds `ladder` inputs for four weekly CETES rungs. Nothing fires
when the rate, the reserve target or a balance is unknown.

**Look-through.** `exposure` fills fund holdings the household lacks from
saved `research.<SYMBOL>` fund packets. Yahoo is used only when market data is
online (`WEALTH_OFFLINE` unset; `live_lookthrough: false` turns it off). The
result adds `top_underlying`, `overlap_matrix`, `lookthrough_sources` and
`employer_concentration` (from `income_exposures[].employer_instrument_id`,
with the salary). A fund with a known asset class labels its unreported
residual with that class.

**Mexico mortgage.** `mx_deductions` takes `mortgage` (LISR Art. 151 fr. IV).
It needs real interest from the lender's constancia, the credit in UDIs, and
confirmation that the loan is for the home and from the financial system. It
counts inside the global cap after `general_mxn`. A credit above 750,000 UDIs
keeps the proportional share.

**Roth conversions.** `retirement_us` `withdrawals` sweeps conversion
ceilings (`conversion_sweep`, 10/12/22/24%) and names a `best_strategy`. It
taxes `social_security_annual_benefit_usd` under IRC 86 (25k/34k single,
32k/44k joint; fixed nominal thresholds deflated at `threshold_inflation`). It
charges IRMAA from the 2026 CMS table on MAGI from two years earlier. Other
years use the latest table with a dated warning.

## Tax pack

`tax_pack` is the document a person hands their contador (Mexico) or CPA (US)
for one year: working papers, not a return. Inputs are `tax_year` (default: the
last completed year) and `jurisdiction` (`MX`, `US` or `MX,US`; default: the
profile's tax residence, plus `US` for a US person). It reads the ledger and
three kinds of saved facts:

- `tax.<year>`: what the person stated for the year. `mx`: Art. 129
  `article_129_loss_carryforwards` (`[]` = none), `total_income_mxn`,
  `accumulable_income_mxn`, `deductions` (`medical_mxn`,
  `insurance_premiums_mxn`, `ppr_mxn`, `art185_mxn`, `mortgage`...),
  `aguinaldo_mxn`/`ptu_mxn`, `sic_listed`, `inpc` (`{"YYYY-MM": value,
  "source"}`). `us`: `filing_status`, `capital_loss_carryover`,
  `ira_contributions_usd`, `roth_contributions_usd`, `rmd_taken_usd`,
  `ira_prior_year_end_balance_usd`, `treasury_rate_per_usd`,
  `mx_annual_isr_usd`.
- `constancia.<id>`: an institution's annual document as printed, with
  `tax_year`, `institution`, `account_id?`, `account_last4?` and blocks `enajenacion`
  (`gain`, `loss`, `net`), `intereses` (`nominal`, `real`, `real_loss`,
  `isr_withheld`), `dividendos`, `form_1099_b`, `form_1099_div`,
  `form_1099_int`. Save it with `wealth_remember` (`source.kind: document`)
  after reading the PDF with the person.
- `client.profile` (residence, tax residence, `us_person`, birth year). Past
  its review date it is left out like any stale fact (warned, and listed to
  reconfirm): the jurisdiction then comes from `tax.<year>` or the request,
  or the pack asks for it (`needs_input`).

Sections, each with `status`, `summary`, a `table` (`columns` in es/en,
`rows`), `reconciliation`, `missing`, `warnings`, `sources` and `assumptions`:

| Section | What it holds |
| --- | --- |
| `mx_enajenacion` | Art. 129 per broker: average cost updated by INPC (CFF Art. 17-A), gains, losses, net, carryforwards, the 10% |
| `mx_extranjero` | Foreign broker: SIC-listed at 10% (criterio 37/ISR/N), others progressive, foreign dividends and the Art. 5 credit (via `mx_foreign`) |
| `mx_intereses` | Nominal and real interest per institution, ISR withheld; debt-security sale gains are interest |
| `mx_dividendos` | Domestic (the additional 10%) and foreign dividends with withholding |
| `mx_deducciones` | Art. 151/185 caps and room (via `mx_deductions`) and the CFDI checklist (uso D01-D10) |
| `mx_aguinaldo_ptu` | Exempt parts (30 and 15 daily UMA), only when stated |
| `us_8949` | Lots sold: boxes A-F, code W and the wash-sale adjustment |
| `us_schedule_d` | Short/long totals, carryover in and out, the $3,000 limit |
| `us_1099` | 1099-DIV/INT per account: ordinary vs qualified, interest |
| `us_foreign_tax` | Foreign tax paid by country (Form 1116 inputs) |
| `us_retirement` | IRA/Roth contributions vs the limit, RMD required and taken |
| `us_fbar_8938` | FBAR ($10,000 aggregate maximum, 31 CFR 1010.350) and Form 8938 (from $50,000/$75,000 single in the US to $400,000/$600,000 joint abroad, Instructions for Form 8938) |

Every pack also has `pendientes` (each missing constancia, INPC month, price,
rate, cost basis or stated figure, in Spanish and English) and `deadlines`
(April 30 annual return in Mexico, February 15 constancias, April 15 US return,
IRA contributions and FBAR, June 15 abroad, October 15 FBAR extended).

Unknown is never zero: a figure that cannot be computed is `null` (an empty
CSV cell, "unknown" on the page). A saved constancia or 1099 is the source of
truth: each section's `reconciliation` shows our computation, the document and
the difference, and the declared figure is the document's. The web app serves
`/api/tax-pack?year=YYYY&format=json|html|csv&section=<id>&lang=es|en`; the You
page has a link to the printable page. `exports: ["csv", "html"]` returns both
inside the result for MCP hosts.

## Estate register

`estate_register` answers "what happens to each account if I die". Each row
is an account, life policy or property with its mechanism: a beneficiary
designation (bank beneficiaries under LIC Art. 56, casa de bolsa under LMV
Art. 201, AFORE under LSS Art. 193, life policies, US TOD/POD and plan
beneficiaries), a trust, survivorship on a US joint account, the will, or
intestate succession. It also lists who receives the account and an estimated
amount per heir. A cuenta mancomunada is not a beneficiary: the co-holder keeps
their own part and yours passes by your designation or your will.

`gaps` are ranked by the amount at risk. They cover no beneficiary, AFORE
beneficiaries, shares that do not add to 100%, a minor named directly without a
guardian or trust, a predeceased or ex-spouse beneficiary, a designation older
than `review_years` (5) or older than a marriage, divorce or child, ERISA
spousal consent for a 401(k), US-situs assets over US$60,000 for a non-resident
alien (through `estate`), no will (with Mes del Testamento in September), a
will older than a marriage or child, and no guardian for minors. What nobody
said (for example an AFORE with no beneficiaries recorded) goes to `questions`
and is never read as "none".

`completeness.score` (0-100) weighs designations by value (60), the will (30)
and a guardian when there are minors (10). The run draws two views: accounts to
heirs with the score, and the amount per heir. `today` shows the top three gaps
as `estate_gap` items once the person has told Wealth anything about their
estate. The You page shows one collapsed Herencia / Estate line. Statutes and
the not-legal-advice caveat are in `sources` and `assumptions`.

Designations are their own facts, so naming a beneficiary never re-dates a
balance: `estate.designation.<slug>`, where the slug is the account key with
`.` as `-` (`investment.gbm` becomes `estate.designation.investment-gbm`). Each
takes `account` (the `cash.`, `investment.`, `insurance.`, `property.` or
statement `account.` key), `beneficiaries` (`[{name, relationship?, share?,
contingent?, minor?, birth_year?, deceased?, via_trust?}]`, where `[]` means
none), `designation_date`, `titling` (`individual`, `joint`, `mancomunada`,
`fideicomiso`, `trust`), `co_owners`, `owner_share`, `country`, `plan_type`,
`spousal_consent` and `marital_property` (false for what was owned before the
marriage or inherited). Beneficiaries saved inline on an account by older
writes are still read; the designation fact wins. Other keys: `insurance.<id>`
(`kind`, `coverage`), `property.<id>` (`kind`, `value`), `estate.will`
(`exists`, `date`, `notaria`, `jurisdiction`, `heirs`), `estate.guardianship`
(`guardian`, `alternate`) and `estate.family` (`marital_status`,
`marriage_date`, `marital_regime`, `spouse_assets`, `spouse`, `children`,
`ex_spouses`, `deceased`, `parents_living`).

Mexican intestate shares follow the Código Civil Federal. Next to
descendants, the spouse takes a child's share only if they own nothing, or
what brings their own property up to a child's share (Arts. 1624-1625). Next
to parents, the spouse takes half whatever they own (Arts. 1626, 1628). Under
sociedad conyugal, half of what was acquired in the marriage is already the
spouse's. It is not in the estate, and it counts as the spouse's own property
for Art. 1624. When the regime or the spouse's property is unknown, amounts
come back as `estate_value_range` and `amount_range`, the fields to ask for
appear in `missing`, and the views draw a range.

```sh
printf '%s' '{"task":"estate_register","inputs":{"as_of":"2026-09-22","facts":[
  {"key":"client.profile","value":{"residence":{"country":"MX"}}},
  {"key":"investment.gbm","value":{"amount":217000,"currency":"MXN","institution":"GBM"}},
  {"key":"estate.designation.investment-gbm","value":{"account":"investment.gbm","beneficiaries":[]}},
  {"key":"estate.will","value":{"exists":false}}]}}' | uv run wealth run
```

## Facts

A fact has `key`, `value`, `source` (`kind`, `ref`, `observed_on`), optional
`confidence` (default `reported`), `expires_on`, `merge` and `valid_from`.
`fact_contract` (in any `context` result) has the full schema.

- `source.kind`: `user` for what the person said, `document` for a supplied
  file, `web` for a page (`ref` is the URL), `tool` for a Wealth result,
  `inference` for an interpretation, `pattern` for something noticed in their
  transactions. Confirmed statements, connector syncs and chat balances are saved with kind
  `document`, `connector` or `user` by Wealth itself. Goals, profile, preferences,
  constraints and tax profile from documents, web pages or connectors are
  stored as `inferred` until the person confirms them.
- Evidence never overwrites what the person said: such a write is held as a
  contradiction and returned in the receipt's `needs_user`.
- `expires_on` defaults to `observed_on` plus the review horizon for the key
  (`store.REVIEW_DAYS`: 30 days for holdings, `account.*`, `liability.*` and
  saved analyses; 90 for `cash.*`, `investment.*`, `income.*`, `spending.*`,
  planning inputs and research; 180 for theses and threads; 365 otherwise).
  Past-review facts stay visible, marked stale, and are excluded from
  calculations and decisions.
- Without `expected_revision`, a write may add new keys or apply `merge: true`
  patches (RFC 7386 for objects; lists of objects merge by `id`). Replacing an
  existing value wholesale requires `expected_revision`. A `null` value forgets
  a key and leaves a tombstone in the history.
- Government IDs, account or card numbers, addresses and credentials are
  rejected.

`remember` returns `client`, `written` (key, id, confidence, expiry, source
kind), `write_result`, `needs_user` and `warnings`; use `inspect` to read values.

## Monitoring

Nothing starts automatically. Rules are opt-in facts (`monitor.rules`). A host
with its own scheduler calls `run` task `monitor`; otherwise `wealth watch`
polls in the foreground. Both return only state changes, and neither sends
notifications, places trades or moves money.
