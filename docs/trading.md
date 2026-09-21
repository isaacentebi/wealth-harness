# Trading

Wealth can place a person's own brokerage orders, starting with Alpaca. The
rule is enforced in code, not in the prompt: **the model can only propose an
order ticket; the person places it by tapping the order card in the app.**
Paper trading is the default.

## How it works

1. **Ticket.** When the person asks to act, the model calls
   `wealth_run order_ticket` with the exact orders, a short rationale and a
   source (`rebalance`, `manager_mirror` or `user_request`). Wealth prices each
   line, runs the pre-trade checks, stores the ticket (auxiliary namespace
   `execution`) and returns a ticket id and a summary. It only reads from
   Alpaca. The result never contains the confirmation code.
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
| Another recent ticket or an open Alpaca order for the same symbol and side; market closed; a passive limit far from the market; a market order (paper only) | warn |
| Prices, account or asset could not be read (no keys, network) | unknown |

A dollar amount becomes a quantity at the limit price (6 decimals if the asset
is fractionable, otherwise whole shares). Wealth never sends `notional`.

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
- Alpaca's own behaviour (fills, rejections, outages) is outside Wealth. Line
  states report it and never assume success.

## Setup

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
keys), the limits and today's live usage. `wealth order_status` with
`{"client_id": "<id>", "refresh": true}` refreshes open orders and posts
fills.

## Files

- `wealth/execution/tickets.py`: tickets, checks, confirmation, refresh, fills,
  cancel.
- `wealth/execution/brokers/alpaca_orders.py`: the Alpaca client, its allowlist
  and the cited Alpaca documentation.
- `wealth/store.py`: the `orders` audit table (added on open, no schema version
  change).
- `wealth/web.py`: `GET /api/orders`, `POST /api/orders/<ticket_id>/confirm`,
  `POST /api/orders/<ticket_id or order_id>/cancel`.
- `wealth/chat.html`: the order card.
- `tests/test_execution.py`, `tests/test_execution_web.py`: all against an
  in-memory fake Alpaca, with no network.
