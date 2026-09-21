# Run the assistant

The optional local launcher uses your installed Codex CLI and its normal login.
Wealth itself is a model-independent MCP server. The local launcher supports Sol
and Luna; it does not implement an OpenRouter chat client or extract OAuth tokens.

## Browser chat

```sh
uv sync
codex login
uv run wealth-chat --client my-profile --model sol
```

Open **http://127.0.0.1:8765/**. `--model` defaults to `sol` and `--client` to
`personal`. Use `--model luna` for Luna. The page streams
progress ("Checking your saved profile", "Searching the web", "Running a stress
test"), can stop a response, and renders headings, lists, tables and source
links. Response depth is Fast, Balanced or Deep (Codex reasoning low, medium,
high); Fast is the default. Native web search is enabled by default; Python
calculations run through Wealth tools, not an unrestricted shell. A small chip
notes when facts were saved to memory. **New conversation** clears the transcript
and Codex session, not memory. Light and dark themes follow the system setting.

Errors appear under the failed message with a Retry button and name the cause:
Codex not installed, signed out (run `codex login`), timed out, model rejected,
or local storage unavailable. If the server restarts, the page reconnects on the
next send; reloading during a response reattaches to it.

Attachments (PDF, CSV, PNG, JPEG, WebP; up to 25 MB, five per message) can be
added with the paperclip or by drag and drop. They are stored in `uploads/<client>/`
next to the database, under server-generated names, and deleted once a statement
is confirmed, after 30 days otherwise, or when the client is deleted (see
[cli.md](cli.md#ingestion) for the settings). The assistant is told each
file's name, type and local path and reads PDF and CSV statements with
`wealth_ingest`; it shows what the statement means and saves it only after you
say yes. Photos need a host that can read images, since the local launcher
turns image viewing off.

Onboarding is a short run of one-question cards (name and residence, age and
dependents, income, spending, savings and investments, debts, goals, risk,
statements) that write facts directly; each can be skipped, and a statement
upload can replace typing. A first synthesis of where you stand follows. Design notes: [onboarding.md](onboarding.md).

**The You page** (`/profile`, linked from the chat) shows what Wealth knows,
where each fact came from and when it was last checked. Facts can be edited,
confirmed or deleted, their history opened, and contradictions between what you
said and a document settled there.

**Order cards** appear when you ask to buy or sell and Wealth prepares an order
ticket. The card shows PAPER or LIVE, every line, and any problem the checks
found; only tapping **Place orders** sends it. See [trading.md](trading.md).

## Memory and fresh tests

The profile ID is an internal storage identifier. The same ID and database resume
saved facts. Relevant explicit facts and corrections are remembered automatically;
you can ask not to save a detail. In the browser chat the answering turn cannot
write facts: after each answer is shown, a separate short Codex run with only
the memory tools records what the exchange established, so saving never delays
an answer. The terminal agent saves during the turn. Hypothetical scenarios are not personal facts.
The browser transcript lasts only for the running server session.

`--db` selects a database; otherwise `WEALTH_DB` or the default local data directory
is used. Both launchers use the same database selection. Databases are plaintext.
Selected context and tool results reach the model provider.

To test onboarding from zero, stop the running chat with Ctrl+C and choose a
**new, unused database path**:

```sh
uv run wealth-chat --client test --model sol --db private/onboarding-test-01.sqlite3
```

Reload the browser after restarting. Reusing that database resumes its memory;
a different unused filename starts empty. This leaves earlier profiles intact.
Never commit a personal database or exported conversation.

## Terminal and fictional demo

```sh
uv run wealth-agent --client my-profile --model sol
uv run wealth-agent --demo --model sol
```

The fictional demo starts with $200,000 in SPY and $100,000 cash. A $24,000
reserve and $50,000 home goal leave $26,000 additional cash to invest. Existing
holdings are not additional cash. Detailed constituents and tax lots are absent.
Demo memory also persists, including corrections. Try changing the home goal
and asking how the available cash changes.

Use `uv run wealth-agent --help` for one-shot prompts and other options;
`uv run wealth-chat --help` lists browser options.

## Runtime and policy

The first turn of a conversation runs `codex exec`; later turns continue that
Codex session with `codex exec resume`, so earlier tool results stay in context.
If a session cannot be resumed, the turn starts fresh with the recent messages
and a short summary of earlier requests. Codex keeps session files under its own
home directory (normally `~/.codex/sessions`); pass `--ephemeral` to either
launcher to keep nothing there, at the cost of that continuity. SQLite remains the
durable financial memory.

Each turn's prompt carries only per-turn context: the date, the browser's time
zone, the `<situation>` brief (the saved picture in numbers), the views of that
picture, which saved facts are current, inferred or past their review date, the
profile ID, attachments, whether web search is on, and the conversation. Standing policy lives
in the instructions file below.

The launcher uses a read-only execution sandbox and turns off Codex features a
financial assistant does not need: shell and unified exec, apps, plugins,
subagents, browser and computer use, image viewing and generation, Codex
memories, tool suggestions and skill search. Wealth tools can write to their
configured database. No transfers, messages or background monitors start, and
the model cannot place an order: only the tap on an order card does. Stopping
a response ends the whole Codex process group, including the Wealth MCP server.

[wealth/instructions.md](../wealth/instructions.md) replaces Codex's built-in
coding instructions; inherited AGENTS files are disabled for this assistant.
The standalone MCP server reads the same policy. The launcher suppresses that
MCP copy so the policy is supplied once. Low response verbosity is independent
of reasoning effort; explicit requests for detail can still receive long answers.

The browser server binds only to a loopback address (`127.0.0.1`, or `::1` with
`--host ::1`), checks the Host and Origin headers, requires a session token on
every request that reads a turn or changes state, and sends no referrer. This is
a local personal interface, not a hosted multi-user service.

## Other model providers

To use OpenRouter or another API provider, configure it in your own host agent,
then attach Wealth through the [MCP configuration](../README.md#connect-your-own-agent).
Keep credentials in that host's secret configuration, outside prompts and Git.
The host supplies web search, scheduling and any embedding service. Wealth does
not supply a hosted model-provider integration or public authentication layer.

Official Codex references: [authentication](https://learn.chatgpt.com/docs/auth),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
