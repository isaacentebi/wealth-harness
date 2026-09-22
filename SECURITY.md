# Security

Wealth holds a person's financial picture and can prepare brokerage orders, so
security reports are welcome and taken seriously.

## Reporting a vulnerability

Report privately through GitHub: on
[the repository](https://github.com/isaacentebi/wealth-harness), open the
**Security** tab and choose **Report a vulnerability**. Please do not open a
public issue for anything exploitable. If private reporting is unavailable,
open an issue that asks for a contact and leave out the details.

Include what you found, how to reproduce it (a failing test is ideal), and what
an attacker gains. Expect an acknowledgement within a week. Fixes land on
`main`; there are no separately maintained release branches.

Especially in scope:

- any way for a model, an MCP host, a statement, a web page or a chat message
  to place, confirm or cancel an order, or to choose live trading or the limits;
- consent bypasses: saving a proposal, accepting a decision or resolving a
  contradiction without the person's own words;
- credential disclosure in results, logs, exports or chat;
- the local web server accepting a request from another origin or host.

## What Wealth defends and how

The full trading threat model, with what a manipulated model or a malicious
page can and cannot do, is in [docs/trading.md](docs/trading.md#threat-model).
Trust boundaries across the whole system are in
[docs/architecture.md](docs/architecture.md#trust-boundaries). In short:

- **Only the person places an order.** The model can prepare an order ticket;
  it is sent only when the person taps **Place orders** on its card in the
  local chat page. No MCP tool, CLI operation or service method submits an
  order. Paper trading is the default; live trading needs a server-side opt-in,
  a typed confirmation the first time, and per-order and daily limits.
- **Local only.** The chat server binds to `127.0.0.1` or `::1` and nothing
  else. It checks the Host and Origin headers (which also stops DNS rebinding)
  and requires a session token for writes. The MCP server speaks stdio only.
  Nothing syncs, trades or monitors in the background.
- **Credentials stay in the keychain.** Broker and bank keys (Interactive
  Brokers Flex, Alpaca, Cuenca) are read from the OS keychain or environment
  variables at call time. They are never tool inputs and are never written to
  the database, logs, exports or the conversation. Connectors only send read
  requests.
- **Data is data.** Statement text, web pages, tool results and recalled facts
  are treated as untrusted content, never as instructions.

## What it does not defend

- The database is a plaintext SQLite file. Anyone who can use your operating
  system account can read it, run the server or read the keychain entries it
  can read. Protect the computer and its account.
- The model provider (OpenAI through Codex, or your own MCP host's provider)
  sees the conversation, a short summary of your situation each turn and the
  tool results the model reads.
- A person can tap **Place orders** on a ticket they did not read. The card
  shows every line, paper or live, and every failed check.
