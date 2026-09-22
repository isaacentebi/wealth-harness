# Run the assistant

The optional local launcher uses your installed Codex CLI and its normal login.
Wealth itself is a model-independent MCP server. The local launcher runs whatever
model your Codex config names; it does not implement an OpenRouter chat client or
extract OAuth tokens.

## Browser chat

```sh
uv sync
codex login
uv run wealth-chat --client my-profile
```

Wealth uses the model in your Codex config (`~/.codex/config.toml`), so a Codex model change moves Wealth too. Pass `--model sol`, `--model luna` or a full model ID to override it.

Open **http://127.0.0.1:8765/**. `--client` defaults to `personal`. The page streams
progress ("Checking your saved profile", "Searching the web", "Running a stress
test") and then the answer itself as it is written, can stop a response, and
renders headings, lists, tables and source links. Response depth is Fast, Balanced or Deep (Codex reasoning low, medium,
high); Fast is the default. Native web search is enabled by default, and stays
off for the rest of a conversation once a file has been read; Python
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
upload can replace typing. A first synthesis of where you stand follows. Design notes: [notes/onboarding.md](notes/onboarding.md).

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
an answer, and the next message does not wait for it either: saves queue in the
background and run one at a time, in order. A bare greeting or thanks ("hola",
"gracias", no numbers) states nothing and skips the step; anything else, even
"ok" or "sí", goes through it. The terminal agent saves during the turn. Hypothetical scenarios are not personal facts.
The browser transcript lasts only for the running server session.

`--db` selects a database; otherwise `WEALTH_DB` or the default local data directory
is used. Both launchers use the same database selection. Databases are plaintext.
Selected context and tool results reach the model provider.

To test onboarding from zero, stop the running chat with Ctrl+C and choose a
**new, unused database path**:

```sh
uv run wealth-chat --client test --db private/onboarding-test-01.sqlite3
```

Reload the browser after restarting. Reusing that database resumes its memory;
a different unused filename starts empty. This leaves earlier profiles intact.
Never commit a personal database or exported conversation.

## Terminal and fictional demo

```sh
uv run wealth-agent --client my-profile
uv run wealth-agent --demo
```

The fictional demo starts with $200,000 in SPY and $100,000 cash. A $24,000
reserve and $50,000 home goal leave $26,000 additional cash to invest. Existing
holdings are not additional cash. Detailed constituents and tax lots are absent.
Demo memory also persists, including corrections. Try changing the home goal
and asking how the available cash changes.

Use `uv run wealth-agent --help` for one-shot prompts and other options;
`uv run wealth-chat --help` lists browser options.

## Runtime and policy

Each turn runs one Codex process. There are two runtimes with the same security
posture; `WEALTH_RUNTIME=exec` or `WEALTH_RUNTIME=appserver` forces one, and by
default (`auto`) Wealth uses app-server and falls back to exec when it cannot
start a turn:

- **app-server** ([wealth/appserver.py](../wealth/appserver.py)) runs `codex app-server`
  and speaks its JSON-RPC protocol over stdio (`initialize`, `config/read`,
  `thread/start` or `thread/resume`, `turn/start`), so the answer arrives as
  `item/agentMessage/delta` chunks and the page shows it as it is written.
- **exec** runs `codex exec --json` (and `codex exec resume`), which sends each
  message whole: the answer appears at once. The memory step after each answer
  always uses exec; nobody watches it.

The first turn of a conversation starts a Codex thread; later turns resume it,
so earlier tool results stay in context. If a thread cannot be resumed, the turn
starts fresh with the recent messages and a short summary of earlier requests.
SQLite remains the durable financial memory.

`codex app-server` has no `--ignore-user-config`, and its `-c` overrides merge
into your `~/.codex/config.toml` instead of replacing it: your other MCP servers,
plugins, notify hook, model provider or base URL would reach the Wealth turn.
And even `codex exec --ignore-user-config` still loads `AGENTS.md` and
`skills/` from its Codex home and `.codex/skills` from its working directory.
So both runtimes run with a Wealth-owned Codex home,
`$XDG_DATA_HOME/wealth-harness/codex-home` (default `~/.local/share/...`,
`WEALTH_CODEX_HOME` overrides it), in a fresh empty 0700 working directory per
turn (removed afterwards; `project_root_markers=[]` keeps Codex from searching
its parents). The home is private (0700), its `config.toml` is rewritten empty
every turn, any `AGENTS.md`, `AGENTS.override.md`, `skills/`, `hooks/`,
`hooks.json` and `rules/` in it are removed every turn, and `auth.json` there is
a symlink to your own Codex login, which Codex rewrites in place, so a token
refresh reaches your real file. The path is resolved once and checked before
anything is written: it must not be, contain or be inside your own Codex home
(compared resolved, case-folded and by inode, so `~/.codex/../.codex` and
`~/.CODEX` count), an existing directory must be one Wealth made, and outside
`$HOME` every parent must belong to you or root and not be writable by others
(a root-owned sticky `/tmp` is fine).

A Codex login kept in the system keyring (no `auth.json`) cannot be shared with
that private home. Either sign in with `CODEX_API_KEY`, switch Codex to file
credentials (`cli_auth_credentials_store = "file"` in `~/.codex/config.toml`,
then `codex login`), or set `WEALTH_CODEX_SHARED_HOME=1`: Wealth then uses
`codex exec` with your own Codex home (still in an empty working directory), so
your `~/.codex` AGENTS.md and skills reach its turns, and answers don't stream.

Before any app-server thread starts, Wealth reads the effective config back
(`config/read` with `includeLayers`) and refuses the turn if any layer but its
own `-c` flags is non-empty (system, managed, MDM or project config), if
`openai_base_url`, another `chatgpt_base_url` or a model provider is set (where
your login token would be sent), if hooks, skills, trusted projects or
`experimental_*` endpoints are set, if anything but the Wealth MCP server is
enabled, the sandbox is not read-only, approvals are not `never`, a disabled
feature is on or a notify hook is set. In `auto` it then uses exec for five
minutes before trying app-server again. Without a shared `auth.json` (signed
out, or credentials in the keychain) neither runtime starts a turn (exec signed
in with `CODEX_API_KEY` needs none): Wealth fails closed rather than run in your
own Codex home. Both runtimes keep their sessions in the Wealth home. Pass
`--ephemeral` to either launcher to keep no session files at all, at the cost of
continuity.

A new app-server process starts for every turn, rather than one kept warm per
chat. The Wealth MCP server reads the turn's consent evidence (the private
`WEALTH_TURN_FILE`) and whether web search is live when its thread starts, and
Codex keeps a loaded thread's MCP servers running; a warm process would carry
the first turn's evidence and search setting into later turns. Starting the
process and completing the handshake (`initialize`, `config/read`) takes about
45 ms, which is all a warm process would save; the MCP server starts per thread
either way.

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
a response ends the whole Codex process group and every descendant, including
the Wealth MCP server, which Codex starts in a process group of its own; the MCP
server also exits by itself as soon as its stdin closes or its parent is gone
(SQLite rolls back any write that had not committed).
Both runtimes get the same overrides, the same scrubbed environment (broker keys,
tokens and other secrets removed) and the same per-turn consent file and
web-search rules; app-server approval requests are declined.

[wealth/instructions.md](../wealth/instructions.md) replaces Codex's built-in
coding instructions; inherited AGENTS files are disabled for this assistant.
The standalone MCP server gives other hosts a compact contract instead and
appends this full policy only with `WEALTH_BEHAVIOR_IN_SERVER=1`; the launcher
sets `WEALTH_BEHAVIOR_IN_HOST=1` so the policy reaches the model exactly once.
The launcher appends a short note naming the tools as Codex shows them
(`mcp__wealth__wealth_run`, `web__run`), saying there is no file or shell tool
and asking for independent calls in one step; the common tasks and their inputs
are in `wealth_run`'s description, and `wealth_context(intent=<task>)` returns
just that task's schema (`detail=full` for the whole catalog). Low response verbosity is independent
of reasoning effort; explicit requests for detail can still receive long answers.

The browser server binds only to a loopback address (`127.0.0.1`, or `::1` with
`--host ::1`), checks the Host and Origin headers, requires a session token on
every request that reads a turn or changes state, and sends no referrer. This is
a local personal interface, not a hosted multi-user service.

## Speed

Most of a turn is model steps, so the launcher keeps them few and small: the
situation is built once per turn, discovery results are summaries by default (a
turn that cannot save facts never receives the ~9k-character fact contract),
and the answer is shown as soon as Codex reports the turn complete, while the
process exits in the background. With app-server the first words show while
the rest is written; with exec the whole answer appears at once.

Measured with Codex 0.153.4 on one profile, a fresh thread per question,
reasoning low and web search on; seconds, two runs each. With exec the first
text is the whole answer.

| Question | exec: answer | app-server: first text | app-server: answer |
| --- | --- | --- | --- |
| hola | 4.9, 6.0 | 3.7, 4.9 | 4.2, 5.6 |
| ¿cómo voy? | 7.7, 8.6 | 4.5, 3.2 | 8.5, 7.1 |
| ¿Me conviene comprar el S&P 500 por el SIC en GBM…? | 31.6, 28.4 | 8.0, 9.7 | 18.6, 19.4 |

`--service-tier fast` on `wealth-chat` or `wealth-agent`, or
`WEALTH_SERVICE_TIER=fast`, asks Codex for its fast tier (priority processing)
for every Wealth turn and memory step. It is off by default because it costs
more; without it Codex uses your account's default tier.

## Other model providers

To use OpenRouter or another API provider, configure it in your own host agent,
then attach Wealth through the [MCP configuration](../README.md#plug-into-your-agent-no-model-needed).
Keep credentials in that host's secret configuration, outside prompts and Git.
The host supplies web search, scheduling and any embedding service. Wealth does
not supply a hosted model-provider integration or public authentication layer.

Official Codex references: [authentication](https://learn.chatgpt.com/docs/auth),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
