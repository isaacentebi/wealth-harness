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

Open **http://127.0.0.1:8765/**. Use `--model luna` for Luna. The page provides
source links, retryable errors and a Low/Medium/High reasoning selector. Low is
the default. Native web search is enabled by default; Python calculations run
through Wealth tools, not an unrestricted shell. File uploads are not implemented.

Onboarding begins with the person's financial situation. Rough figures and
partial answers are welcome; goals and commitments develop through conversation.
A specific investment question can be explored alongside onboarding.

## Memory and fresh tests

The profile ID is an internal storage identifier. The same ID and database resume
saved facts. Relevant explicit facts and corrections are remembered automatically;
you can ask not to save a detail. Hypothetical scenarios are not personal facts.
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

Each turn runs an ephemeral `codex exec` with bounded recent conversation.
SQLite owns durable financial memory. The launcher uses a read-only execution
sandbox with shell, apps, plugins and subagents disabled. Wealth tools can write
to their configured database. No trades, transfers or background monitors start.

[wealth/instructions.md](../wealth/instructions.md) replaces Codex's built-in
coding instructions; inherited AGENTS files are disabled for this assistant.
The standalone MCP server reads the same policy. The launcher suppresses that
MCP copy so the policy is supplied once. Low response verbosity is independent
of reasoning effort; explicit requests for detail can still receive long answers.

The browser server binds only to localhost, checks origins and uses a session
token. This is a local personal interface, not a hosted multi-user service.

## Other model providers

To use OpenRouter or another API provider, configure it in your own host agent,
then attach Wealth through the [MCP configuration](../README.md#connect-your-own-agent).
Keep credentials in that host's secret configuration, outside prompts and Git.
The host supplies web search, scheduling and any embedding service. Wealth does
not supply a hosted model-provider integration or public authentication layer.

Official Codex references: [authentication](https://learn.chatgpt.com/docs/auth),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
