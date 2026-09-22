# Trading

Wealth can place a person's own brokerage orders at Alpaca and at Interactive
Brokers, and prepares an exact "place this yourself" card for every other
broker. The rule is enforced in code, not in the prompt: **the model can only
propose an order ticket; the person places it by tapping the order card in the
app.** Paper trading is the default.

## Brokers

The account on the ticket's orders (`orders[].account_id`) picks the broker,
by that account's institution in the ledger (or its `account.<id>` fact). The
institution must be exactly one of the aliases in the canonical table in
`base.py` ("Interactive Brokers", "Interactive Brokers LLC", "IBKR", "Alpaca
Securities", ...) to reach an API broker; a longer name that merely contains
one ("GBM (not Interactive Brokers)") is manual:

| Institution | Adapter | What the tap does |
|---|---|---|
| Alpaca | `alpaca` (Trading API v2) | sends the orders (paper unless opted in) |
| Interactive Brokers | `ibkr` (Client Portal gateway on this computer) | sends the orders to the account the gateway is logged into |
| Anything else (GBM, Vest, Schwab without OAuth, Fidelity, ...) | `manual` | records "Ya la puse / I placed it"; nothing is sent |

A ticket holds one account's orders. When the orders name no account, the
broker the person named picks it ("compra 10 VOO en GBM": their GBM account),
else their only brokerage account; with several candidates the result is
`needs_input` listing the accounts, so the model asks. Only a client with no
brokerage account on record at all falls back to Alpaca (paper by default). An
account Wealth has no record of, whose name says nothing about its broker,
also goes to Alpaca, with a quiet line saying so (`account_unmatched`; on a
live account it blocks); an unrecorded id that names a broker (`gbm-4321`,
`vest`, `ibkr-4567`) goes to that broker.

**Facts never route a live order on their own.** An `account.<id>` fact is
something the model can write, so for Alpaca and IBKR the account the orders
name must be the broker's own ledger account (`account_state().ledger_account_id`:
`alpaca-<last4>`, `ibkr-<last4>`, the ids the connectors use). If the broker
is logged into any other account, the ticket blocks (`account_mismatch`) and
nothing is sent. The same holds for a ledger account at that institution.

All three sit behind one small interface, `OrderBroker`
(`wealth/execution/brokers/base.py`): account state (active, cash buying
power, ledger account), market clock, instrument, latest trade, positions,
open orders, submit, order status, cancel, fills and the ledger account.
The ticket code (`tickets.py`) only speaks that interface, so every check
below runs the same way for every broker; adding a broker is one adapter
module and one line in `kind_for_institution`.

## How it works

1. **Ticket.** When the person asks to act, the model calls
   `wealth_run order_ticket` with the exact orders, a short rationale and a
   source (`rebalance`, `manager_mirror` or `user_request`). Wealth prices each
   line, runs the pre-trade checks, stores the ticket (auxiliary namespace
   `execution`) and returns a ticket id and a summary. It only reads from
   the broker. The result never contains the confirmation code.
2. **Card.** After the answer, the chat page shows the order card: PAPER or
   LIVE, the confirmation code, the total as one number, one line per order
   (side, quantity, symbol, limit, account), warnings and violations as quiet
   lines, estimated tax and cost when known, one vermilion **Place orders /
   Enviar órdenes** button and a text **Cancel / Cancelar**.
3. **Confirmation.** The tap sends `POST /api/orders/<ticket_id>/confirm` with
   the card's code. The server checks the session token and local Host/Origin
   (as for every write), the code, that the ticket is unexpired (10 minutes)
   and unused, then runs every check again on fresh data. The tap confirms
   what the card showed, not just the ticket id: if re-pricing moved a line's
   price or limit by more than the collar (the fresh price is compared with the
   price the card displayed, so a limit the person gave does not hide a moved
   market), or its quantity or amount by more than 2%, or pushed a live amount
   over the per-order limit, nothing is sent. The
   request fails with `price_moved` (409), the card shows the new lines with a
   quiet note, and a fresh tap places them. Only then does it send the
   orders. A "yes" in chat never places anything.
4. **After.** Each line shows its state (Sent / Enviada, Partly filled, Filled /
   Ejecutada, Rejected / Rechazada, Cancelled). The page refreshes open orders
   every 5 seconds, and **Cancel open orders** cancels the ones still working.
   Fills are posted to the ledger.

The model reads a ticket's state with `order_ticket` and only `ticket_id`. It
must not say an order was placed or filled until the line state says so.

A pending ticket past its 10 minutes is settled as `expired` (its code is
dropped) when the tickets are next listed or a new ticket is stored. The
stored list keeps at most 60 tickets, dropping the oldest settled ones first;
the `orders` audit table keeps their history.

## Pre-trade checks

Each check is stored on the ticket with a status. `warn` is a quiet line.
`violation` (the accepted IPS) blocks unless the person ticks the override on
the card, and the override is recorded. `block` is never overridable.
`unknown` blocks too, because an unknown is never assumed to be fine.

| Check | Result |
|---|---|
| Asset tradable and active; fractional quantity only if `fractionable`; fractional orders are `day` only | block |
| Buys fit cash (`non_marginable_buying_power`); Wealth never uses margin | block |
| Sells never exceed the shares held (no shorting) | block |
| Account active and not blocked | block |
| Limit price within the collar (default 1% through the last trade); a missing limit defaults to half the collar | block |
| Live: limit orders only, USD 1,000 per order, USD 5,000 per day (US Eastern date) | block |
| Accepted IPS (`policy.check`: bands, concentration, exclusions, leverage, reserve) | violation or warn |
| Guardrails: the cool-off flag (late at night, right after a large move) | warn |
| Same symbol and side twice in a ticket | block |
| Another recent ticket or an open order at the broker for the same symbol and side; market closed; a passive limit far from the market; a market order (paper only) | warn |
| Prices, account or asset could not be read (no keys, gateway down, network) | unknown |
| The price is not current: an IBKR `C` (prior close) price while the market is open, delayed, frozen or unlabelled market data, a halt, or a last trade older than 15 minutes (market open) / one trading day (closed) | unknown (`price_stale`) |
| Alpaca/IBKR: the broker's own account is not the ledger account the orders name | block (`account_mismatch`) |
| An account Wealth has no record of | warn on paper, block live (`account_unmatched`) |
| IBKR: the gateway is logged into a live account and `WEALTH_TRADING_LIVE` does not opt IBKR in | block |

A dollar amount becomes a quantity at the limit price (6 decimals if the asset
is fractionable, otherwise whole shares). Wealth never sends `notional`.
Live limits and the daily reservation are shared across Alpaca and IBKR.

On a place-it-yourself (manual) card the same checks run on Wealth's own data,
and what Wealth cannot see there is a quiet line rather than a stop, because
the person is looking at their broker's screen: cash from the ledger (warn if
unknown or short), holdings from the ledger (a sale above them still blocks;
no holdings on record is a warn), the limit against the last close (warn: a
close is not a live quote), fractional quantities (warn). A missing price is a
warn too: the line says **precio al momento de colocar** (limit at the price
the broker shows when placing), shows no estimated amount (or a ±10% range
from an older close, with its date), and never blocks. The live limits do not
apply: Wealth sends nothing.

## Submission, audit and fills

- `client_order_id` is `wealth-<ticket_id>-<line>`. Before posting, Wealth
  looks the id up. After a timeout, a 5xx or a duplicate-id rejection, it looks
  it up again. A retry never creates a second order.
- Every request and response goes into the append-only `orders` table, redacted:
  no headers, no keys, the account number masked to its last four digits, and
  no confirmation code. SQLite triggers refuse `UPDATE` and `DELETE`. Rows go
  only when the person's profile is deleted. Export includes them.
- Fills come from Alpaca's `FILL` activities. They are posted with
  `wealth.ledger.post` to account `alpaca-<last4>`, with the same external id
  the read-only connector uses (`ALPACA-A<date>_<uuid>`). Either side can sync
  first, and the other is recognised as a duplicate.
- The order client may send only `POST /v2/orders`, `DELETE /v2/orders/{id}`
  and GETs on a fixed list. Cancel-all, closing positions, account
  configuration and transfers are refused before a request is built. The
  read-only connector in `wealth/connectors/` stays GET-only.

## Threat model

**A confused or manipulated model** (for example, prompt injection in a
statement or web page it read) **can:**

- propose a ticket, including a bad one. It appears on a card with its checks.
  Nothing happens unless the person taps Place orders.
- read ticket states and the trading status (mode, whether keys exist, limits).

**It cannot:**

- place, confirm or cancel an order. No MCP tool, CLI operation or
  `WealthService` method submits. The order-sending code is reachable only from
  the web route, and tests assert this. The one MCP write, `order_ticket
  {ticket_id, placed: true}`, only marks a place-it-yourself ticket as placed
  by the person; it refuses Alpaca and IBKR tickets and never calls a broker.
  It needs the person's own words in a Wealth turn ("ya la puse", "ya lo
  compré", "I placed it") or, in another host, the two-step `needs_person`
  code.
- obtain the confirmation code. The code is not in any `order_ticket` result,
  status read or export. The web page gets it with the session token.
- choose live trading, the broker URL or the limits. These come from the
  server's environment. `order_ticket` rejects unknown fields such as `mode`,
  `nonce` or `confirm`.
- lift a hard block. The IPS override is a checkbox on the card, not an input.
- get an order placed by writing "confirmed" or "sent" in chat. A "yes" in chat
  does nothing, and instructions forbid claiming a placement before the status
  shows it.

**A malicious web page in the person's browser cannot:**

- post to the confirm route. Writes need the `X-Wealth-Token` header, which a
  cross-origin page cannot set without a CORS preflight that the server never
  approves. The Host and Origin must be the loopback address, which also
  defeats DNS rebinding. The code and the 10-minute expiry are further
  barriers.
- read the token, the tickets or the code. There are no CORS headers, and
  `/api/orders` needs the token.
- frame the page to trick a tap (`frame-ancestors 'none'`).

**What remains:**

- Anyone with the person's local account can run the server or read the local
  database. Protect the computer; keys live in the OS keychain.
- A person may tap Place orders on a ticket they did not read. The card shows
  every line, PAPER or LIVE, and every problem. Live trading needs an explicit
  opt-in, a typed confirmation the first time and small default limits.
- The broker's own behaviour (fills, rejections, outages) is outside Wealth.
  Line states report it and never assume success.
- IBKR: anything that can reach `https://localhost:5000` as the person can use
  their logged-in gateway session directly; that is IBKR's design, not
  Wealth's. Wealth adds nothing to it: no credential, and requests only on its
  allowlist. A page in the browser cannot drive the gateway through Wealth.
- Manual cards: "Ya la puse" records an intention, not a trade. Only a ledger
  entry from a statement or sync turns it into `filled`.

## Interactive Brokers (Client Portal gateway)

Wealth talks to IBKR's **Client Portal Web API** through the gateway the person
runs on their own computer. Wealth stores no IBKR credential; every request
rides on the gateway's logged-in session, and the only address it calls is
`https://localhost:<port>/v1/api` (port `WEALTH_IBKR_GATEWAY_PORT`, default
5000; the host is always `localhost`).

**Paper or live is the account the gateway is logged into**, read at every
ticket and again at every tap. Account ids starting with `DU` (or `DF`, a
paper advisor master account) are paper. Any
other account is live: the ticket blocks unless `WEALTH_TRADING_LIVE` names
`ibkr` (or is `1`, which opts in IBKR only; Alpaca always needs its name,
because its live keys may exist just for syncing). Live IBKR orders follow the
same rules as live Alpaca: limit orders only, the per-order and daily caps,
the typed **LIVE** the first time. If the gateway is logged into a different
account at the tap than when the card was drawn, nothing is sent
(`account_changed`). The ticket stores an HMAC-SHA256 of the account id keyed
with a random per-profile key kept in the database (`fingerprint_key` in the
`execution` namespace), never a plain hash a seven-digit id could be
brute-forced from.

Requests (the allowlist in `ibkr_gateway.py`; anything else is refused before
a request is built and again in the transport):

- `GET /iserver/auth/status`, `GET /iserver/accounts` (the selected account)
- `GET /iserver/secdef/search?symbol=&secType=STK` (the conid of a US listing)
- `GET /iserver/marketdata/snapshot` (fields 31 last price, 84 bid, 86 ask,
  6509 market-data availability; the first call subscribes)
- `GET /portfolio/accounts`, `GET /portfolio/{acct}/ledger` (USD cash; the
  lower of cash and settled cash, never margin), `GET /portfolio/{acct}/positions/0`
- `POST /iserver/account/{acct}/orders` (one order, `cOID` =
  `wealth-<ticket>-<line>`, `LMT`, `DAY`/`GTC`, whole shares, no outside-hours)
- `POST /iserver/reply/{id}`, `GET /iserver/account/orders`,
  `GET /iserver/account/order/status/{id}`, `DELETE /iserver/account/{acct}/order/{id}`

**Price freshness.** The snapshot has no documented last-trade time field
(`_updated` is when the gateway's cache changed, not when the last trade
printed; 7295 is the day's open price, not a time). So a price counts as
current only when field 6509 says real-time (`R`). Delayed (`D`), frozen
(`Z`), delayed-frozen (`Y`), not subscribed (`N`) or missing availability
while the market is open is `price_stale`. A `C`-prefixed price is the prior
session's close: stale while the market is open, dated that session's 16:00
New York close otherwise; `H` (halted) is never fresh. These field ids come
from IBKR's Client Portal field list; re-check them before relying on them,
and anything unrecognised is treated as not fresh.

**Order replies.** IBKR may answer an order with a question that must be
confirmed. Wealth confirms none of them inside the tap: every question is
answered `confirmed: false`, the order is not placed, the line shows `Not
sent` with IBKR's words, and the audit table keeps them (`reply_blocked`).
That includes `o163` (the limit exceeds the account's price-percentage limit:
with Wealth's 1% collar, receiving it means Wealth's price disagreed with
IBKR's), `o354`, `o403`, `o10151`, `o10153`, `o10331`, `o2137`, `o10334` (the
order would go to another account, such as an omnibus one), `p6` and `p12`.
`o383` and `o451` are the person's own TWS precautionary limits (order size,
total value); the line adds that they can place the order in TWS themselves or
adjust their presets.

**Order listing.** `GET /iserver/account/orders` answers the first call of a
gateway session with an incomplete list. Wealth reads it at least twice and
until `snapshot` is true (at most four reads), and a single silent listing
never marks a line `failed`: it stays `unknown` and is retried on the next
refresh; only two refreshes at least two minutes apart that both miss it mark
it not placed. An IBKR "duplicate cOID" rejection means the order exists.

**Fills** are not posted to the ledger from the order path: the Client Portal's
execution ids are not the trade ids the Flex connector and statements use, so
posting both would double-count. The line's state and average price come from
order status; the trade reaches the ledger with the next Flex sync or
statement.

**The certificate.** The gateway serves a self-signed certificate. Choose one:

- pin it: `export WEALTH_IBKR_GATEWAY_CERT_SHA256=<sha256 of the DER cert>`;
  the connection is refused unless the gateway presents exactly that
  certificate. To read the fingerprint:
  ```sh
  openssl s_client -connect localhost:5000 </dev/null 2>/dev/null \
    | openssl x509 -outform DER | shasum -a 256
  ```
- or allow it unverified for the loopback address only:
  `export WEALTH_IBKR_GATEWAY_INSECURE_LOCALHOST=1`. The connection then goes to
  the loopback literal (`127.0.0.1`, else `::1`) with `Host: localhost`, never to
  whatever `localhost` resolves to. A pinned certificate does the same.

With neither, the certificate must verify against the system trust store (the
stock gateway's does not), and the card says which setting to add.

### Setup

1. Download the Client Portal Gateway from IBKR (it needs Java), unzip it and
   start it: `bin/run.sh root/conf.yaml`.
2. Open https://localhost:5000 and log in with the **paper** user (its account
   id starts with `DU`). Keep the gateway running; its session times out after
   a period without requests and must be logged into again.
3. Pin the certificate or allow it for localhost (above).
4. Record the IBKR account in Wealth (an IBKR Flex sync or a statement does it,
   with institution "Interactive Brokers"), and ask Wealth for an order on that
   account. Cards say **Interactive Brokers · ****4567 · Practice**.
5. For live: log the gateway into the live user, `export WEALTH_TRADING_LIVE=ibkr`
   (the limits above apply), and type **LIVE** on the first live card.

## Brokers without an API (place it yourself)

For GBM, Vest, Schwab without OAuth, Fidelity or any institution Wealth does
not trade through, the ticket becomes a **place this yourself** card. The same
ticket, checks, code, expiry (60 minutes here) and price-moved rule apply, and
nothing is ever sent anywhere.

The card shows, and its **Copy / Copiar** button copies:

- the broker and account;
- per order: side, quantity, the exact symbol and listing. At a Mexican broker
  a foreign security is bought **in the SIC** ("Compra en el SIC") and a
  Mexican one on the BMV (or BIVA); `orders[].exchange` says which, else the
  ledger's instrument venue, else SIC for anything that is not a known Mexican
  issuer (with a quiet line saying it was assumed). A US broker trades the US
  listing;
- order type, limit (from the last close in Wealth's market-data cache, with
  its date: check the live quote before placing) and time in force;
- the estimated amount in the account's currency, estimated fees (GBM: "hasta
  0.25% + IVA", with GBM+'s minimum commission of MXN 20 per trade, "estimado";
  other Mexican brokers about 0.25% + IVA; US brokers no commission, all shown
  as estimates) and, for a peso ticket, the USD/MXN rate with its date and the
  USD equivalent. A dollar account at GBM ("Trading USA", currency USD) trades
  the US listing in USD: it is priced from a USD close, and without one the
  price is unknown with that reason (Wealth never converts a peso price).

The person places the order at their broker and taps **Ya la puse / I placed
it**. In another chat host (no card), the person tells the model, which calls
`wealth_run task=order_ticket inputs {ticket_id, placed: true}`; that returns
`needs_person` with a summary and code, and only the person's yes completes it
(`confirm: true, confirmation_code` inside `inputs`). In a Wealth turn the
person's own message must say they placed it. Either way the ticket records
`placed_via` (`app` or `mcp`) and `verified: false` until reconciliation. The
ticket becomes `placed` and its lines `awaiting` ("Placed by you").
Every refresh (each time the cards load, or `order_status` with `refresh`)
matches them against the ledger: a buy or sell of the same symbol in that
account (or, if the ticket's account is not in the ledger, any account at the
same institution), dated from the day before the tap on, not already claimed
by another ticket. An entry for more than the order's quantity (beyond 2%) is
a different trade and never fills the line (the line says so). One entry for
the whole quantity is preferred (the closest wins); otherwise smaller entries
add up without overshooting. Within 2% of the quantity the line is `filled`
with the statement's average price and the entry ids it matched, and the
ticket `verified`; below it, `partial`. A line no statement shows within 30
days is `unconfirmed`; a partial one is `partial_unconfirmed`, never `filled`. **I did not place it /
No la puse** withdraws a placed ticket.

## Alpaca setup

### Paper (default)

1. Create a paper account at Alpaca and generate **paper** API keys.
2. Store them in the keychain the read-only connector uses and mark them as
   paper keys:
   ```sh
   security add-generic-password -s wealth-alpaca -a key_id -w '<paper key id>'
   security add-generic-password -s wealth-alpaca -a secret -w '<paper secret>'
   export WEALTH_ALPACA_PAPER=1
   ```
   You can also use `WEALTH_ALPACA_KEY_ID` / `WEALTH_ALPACA_SECRET`. If those
   hold live keys for syncing, add separate paper keys with the keychain
   service `wealth-alpaca-paper`, or with `WEALTH_ALPACA_PAPER_KEY_ID` /
   `WEALTH_ALPACA_PAPER_SECRET`.
3. Start the app (`uv run wealth-chat`) and ask Wealth to buy something. Every
   card says **PAPER · practice**.

### Live

1. Generate **live** keys and store them under `wealth-alpaca` (or the env
   variables) *without* `WEALTH_ALPACA_PAPER`.
2. Opt in for this broker in the server's environment:
   `export WEALTH_TRADING_LIVE=alpaca`. Without it, every ticket is paper, and
   a pending live ticket can no longer be placed. Orders already sent live
   stay reachable: refresh and cancel still reconcile them, post their fills
   and can cancel one that is still open.
3. Optional limits (USD, live only): `WEALTH_TRADING_MAX_ORDER_USD` (default
   1000) and `WEALTH_TRADING_MAX_DAILY_USD` (default 5000).
   `WEALTH_TRADING_COLLAR` (default 0.01) sets how far a limit may sit through
   the last trade, in any mode.
4. The first live card asks the person to type **LIVE** (or **EN VIVO**). After
   that, live cards say **LIVE · real money** and need only the tap.

To check the setup, run `wealth execution_status` with
`{"client_id": "<id>"}`. It shows the mode, whether keys exist (never the
keys), the limits and today's live usage, and under `brokers` the IBKR gateway
address, its live opt-in and certificate policy, and how many manual tickets
await a statement. It reads no broker. `wealth order_status` with
`{"client_id": "<id>", "refresh": true}` refreshes open orders and posts
fills.

## Files

- `wealth/execution/tickets.py`: tickets, checks, confirmation, refresh, fills,
  cancel.
- `wealth/execution/brokers/base.py`: the `OrderBroker` interface and which
  adapter an institution uses.
- `wealth/execution/brokers/alpaca_orders.py`: the Alpaca client, its allowlist
  and the cited Alpaca documentation, and its `AlpacaBroker` adapter.
- `wealth/execution/brokers/ibkr_gateway.py`: the IBKR gateway adapter, its
  allowlist, TLS policy, reply handling, snapshot fields and freshness.
- `wealth/execution/brokers/manual.py`: place-it-yourself tickets (listing,
  reference prices, fees, FX).
- `wealth/store.py`: the `orders` audit table (added on open, no schema version
  change).
- `wealth/web.py`: `GET /api/orders`, `POST /api/orders/<ticket_id>/confirm`,
  `POST /api/orders/<ticket_id or order_id>/cancel`.
- `wealth/chat.html`: the order card.
- `tests/test_execution.py`, `tests/test_execution_web.py`: all against an
  in-memory fake Alpaca, with no network.
- `tests/test_execution_brokers.py`: the IBKR gateway (a fake transport: the
  happy path, the reply handshake, an unknown reply refused, paper/live gating,
  a switched login, the price moved, cancel) and manual tickets with their
  later reconciliation.
- `tests/test_execution_adversarial.py`: the adversarial review's regressions
  (replies, freshness, routing, the order listing, reconciliation, MCP
  placement, fingerprint, loopback transport, GBM fees).
