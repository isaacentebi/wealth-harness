# Trading

Wealth can place a person's own brokerage orders at Alpaca and at Interactive
Brokers, and prepares an exact "place this yourself" card for every other
broker. The rule is enforced in code, not in the prompt: **the model can only
propose an order ticket; the person places it by tapping the order card in the
app.** Paper trading is the default.

## Brokers

The account on the ticket's orders (`orders[].account_id`) picks the broker,
by that account's institution in the ledger (or its `account.<id>` fact):

| Institution | Adapter | What the tap does |
|---|---|---|
| Alpaca | `alpaca` (Trading API v2) | sends the orders (paper unless opted in) |
| Interactive Brokers | `ibkr` (Client Portal gateway on this computer) | sends the orders to the account the gateway is logged into |
| Anything else (GBM, Vest, Schwab without OAuth, Fidelity, ...) | `manual` | records "Ya la puse / I placed it"; nothing is sent |

A ticket holds one account's orders. Orders without an account go to Alpaca,
as before. An account Wealth has no record of, whose name says nothing about
its broker, also goes to Alpaca, with a quiet line saying so; an unrecorded id
that names a broker (`ibkr-main`, `gbm-4321`, `vest`) goes to that broker.

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
   limit by more than the collar, or its quantity or amount by more than 2%,
   or pushed a live amount over the per-order limit, nothing is sent. The
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
| IBKR: the gateway is logged into a live account and `WEALTH_TRADING_LIVE` does not opt IBKR in | block |

A dollar amount becomes a quantity at the limit price (6 decimals if the asset
is fractionable, otherwise whole shares). Wealth never sends `notional`.
Live limits and the daily reservation are shared across Alpaca and IBKR.

On a place-it-yourself (manual) card the same checks run on Wealth's own data,
and what Wealth cannot see there is a quiet line rather than a stop, because
the person is looking at their broker's screen: cash from the ledger (warn if
unknown or short), holdings from the ledger (a sale above them still blocks;
no holdings on record is a warn), the limit against the last close (warn: a
close is not a live quote), fractional quantities (warn). A missing price with
no limit still blocks: the card must say a price. The live limits do not
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
  the web route, and tests assert this.
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
ticket and again at every tap. Account ids starting with `DU` are paper. Any
other account is live: the ticket blocks unless `WEALTH_TRADING_LIVE` names
`ibkr` (or is `1`, which opts in IBKR only; Alpaca always needs its name,
because its live keys may exist just for syncing). Live IBKR orders follow the
same rules as live Alpaca: limit orders only, the per-order and daily caps,
the typed **LIVE** the first time. If the gateway is logged into a different
account at the tap than when the card was drawn, nothing is sent
(`account_changed`).

Requests (the allowlist in `ibkr_gateway.py`; anything else is refused before
a request is built and again in the transport):

- `GET /iserver/auth/status`, `GET /iserver/accounts` (the selected account)
- `GET /iserver/secdef/search?symbol=&secType=STK` (the conid of a US listing)
- `GET /iserver/marketdata/snapshot` (field 31, last price; the first call
  subscribes, a `C` prefix is the previous close, `H` a halt)
- `GET /portfolio/accounts`, `GET /portfolio/{acct}/ledger` (USD cash; the
  lower of cash and settled cash, never margin), `GET /portfolio/{acct}/positions/0`
- `POST /iserver/account/{acct}/orders` (one order, `cOID` =
  `wealth-<ticket>-<line>`, `LMT`, `DAY`/`GTC`, whole shares, no outside-hours)
- `POST /iserver/reply/{id}`, `GET /iserver/account/orders`,
  `GET /iserver/account/order/status/{id}`, `DELETE /iserver/account/{acct}/order/{id}`

**Order replies.** IBKR may answer an order with a question that must be
confirmed. Wealth confirms it inside the person's single tap only when every
message id is known to be benign: `o163` (limit far from the market; the
collar is tighter), `o383` (size limit) and `o451` (value limit; both covered
by Wealth's caps). Any other message is answered `confirmed: false`, the order
is not placed, the line shows `Not sent` with IBKR's words, and the audit
table keeps them (`reply_blocked`).

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
  `export WEALTH_IBKR_GATEWAY_INSECURE_LOCALHOST=1`.

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
- the estimated amount in the account's currency, estimated fees (GBM: 0.25% +
  16% IVA; other Mexican brokers about the same; US brokers no commission, all
  shown as estimates) and, for a peso ticket, the USD/MXN rate and the USD
  equivalent.

The person places the order at their broker and taps **Ya la puse / I placed
it**. The ticket becomes `placed` and its lines `awaiting` ("Placed by you").
Every refresh (each time the cards load, or `order_status` with `refresh`)
matches them against the ledger: a buy or sell of the same symbol in that
account (or, if the ticket's account is not in the ledger, any account at the
same institution), dated from the day before the tap on, not already claimed
by another ticket. Entries add up; within 2% of the quantity the line is
`filled` with the statement's average price and the entry ids it matched. A
line no statement shows within 30 days is `unconfirmed`. **I did not place it /
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
  allowlist, TLS policy and benign reply ids.
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
