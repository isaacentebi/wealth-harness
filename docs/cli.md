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
server to the named tools, and `WEALTH_BEHAVIOR_IN_HOST=1` leaves the
conversation policy out of its instructions when the host already has it.

## MCP tools

Nine tools. `wealth_context`, `wealth_recall` and `wealth_inspect` are
read-only; none is destructive. Arguments are strict: unknown fields are
rejected, and errors name the field and the expected inputs.

| Tool | What it does | Example arguments |
| --- | --- | --- |
| `wealth_context` | Task schemas without `client_id`; with it, the facts relevant to a task, or the whole picture with `intent="situation"` | `{"intent": "debt_payoff"}` |
| `wealth_client` | `create` the profile once; `index` a host-supplied embedding for a fact | `{"action": "create", "client_id": "ana", "inputs": {"display_name": "Ana"}}` |
| `wealth_remember` | Save sourced facts, corrections or `merge` patches atomically; returns a receipt with `needs_user` | `{"client_id": "ana", "facts": [{"key": "spending.monthly", "value": {"total": 30000, "currency": "MXN"}, "source": {"kind": "user", "ref": "conversation", "observed_on": "2026-09-21"}}]}` |
| `wealth_recall` | Search all remembered facts | `{"client_id": "ana", "query": "spending"}` |
| `wealth_run` | Run one task; optional `save_as` with `expires_on` | `{"task": "debt_payoff", "inputs": {"monthly_amount": 3000, "liabilities": [{"id": "card", "balance": 18000, "annual_rate": 0.42, "monthly_payment": 1200, "currency": "MXN"}]}}` |
| `wealth_inspect` | Current facts (`key`/`keys`), one key's `history`, pending `contradictions`, or an `export` | `{"client_id": "ana", "key": "spending.monthly", "detail": "history"}` |
| `wealth_resolve_contradiction` | Save the person's answer (`keep`, `use_new`, `changed`) to a contradiction | `{"client_id": "ana", "contradiction_id": "<id from needs_user>", "choice": "keep"}` |
| `wealth_ingest` | Uploads, extractions, stated balances and connector syncs into a held proposal; `confirm` saves it after the person's yes | `{"client_id": "ana", "action": "connector_status", "inputs": {}}` |
| `wealth_decision` | Propose, accept or dismiss an evidence-bound decision; acceptance is not execution | `{"action": "propose", "client_id": "ana", "inputs": {"title": "Pay the card first", "rationale": "42% costs more than any safe return", "expected_revision": 1, "evidence_ids": ["<fact id>"]}}` |

## CLI commands

Every command reads one JSON object from stdin or `--input request.json` and
prints JSON; `--db` overrides `WEALTH_DB`. Errors go to stderr as
`{"error", "error_type"}` with exit code 2.

| Command | MCP equivalent | What it does |
| --- | --- | --- |
| `context` | `wealth_context` | Task schemas or relevant facts |
| `client` | `wealth_client`, `wealth_inspect` | `create`, `inspect`, `export`, `index`; `forget` in a terminal only |
| `remember` | `wealth_remember` | Save facts |
| `recall` | `wealth_recall` | Search facts |
| `run` | `wealth_run` | Run a task |
| `decision` | `wealth_decision` | Propose, accept, dismiss |
| `ingest` | `wealth_ingest` | Proposals and confirmation |
| `history` | `wealth_inspect` `detail=history` | One key's timeline |
| `contradictions` | `wealth_inspect` `detail=contradictions` | Pending contradictions |
| `resolve_contradiction` | `wealth_resolve_contradiction` | The person's answer |
| `execution_status` | none | Trading mode, whether keys exist, limits, today's usage |
| `order_status` | none | Order tickets and line states; `refresh` reads the broker |
| `prices` | none | Market-data cache: `status`, or `refresh` now |
| `forget` | none | Delete a profile; interactive terminal only |
| `watch` | none | Foreground monitor polling |
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

Accept or dismiss later with `{"action":"accept","client_id":"ana","inputs":{"decision_id":"..."}}`.

**ingest** turns a statement or stated balances into a held proposal; confirm
only after the person's yes:

```sh
PID=$(printf '%s' '{"client_id":"ana","action":"chat","inputs":{"currency":"MXN","as_of":"2026-09-21",
  "items":[{"kind":"cash","label":"Nu","amount":40000,"quote":"tengo unos 40 mil en Nu"}]}}' \
  | uv run wealth ingest | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["proposal_id"])')
printf '%s' "{\"client_id\":\"ana\",\"action\":\"confirm\",\"inputs\":{\"proposal_id\":\"$PID\"}}" | uv run wealth ingest
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
printf '%s' "{\"client_id\":\"ana\",\"contradiction_id\":\"$CID\",\"choice\":\"keep\"}" | uv run wealth resolve_contradiction
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
day fetched after it closed is final. `WEALTH_OFFLINE=1`, or a failed fetch,
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

**watch** evaluates the client's opt-in monitor rules and prints only changed
events; it runs in the foreground until stopped (`--interval` seconds, default
300), sends no notifications and places no trades:

```sh
uv run wealth watch --client ana --once
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
caller) as `account.<id>`, `liability.<id>` and `income.<id>` facts and posts
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
