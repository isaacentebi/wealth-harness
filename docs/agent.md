# Try Wealth with a real agent

The optional `wealth-agent` terminal connects your existing Codex login to
Wealth's six MCP tools. Choose Sol or Luna and talk naturally. The financial
server remains independent of the model provider.

## Browser chat

```sh
uv run wealth-chat --client isaac --model sol
```

Open [Wealth chat](http://127.0.0.1:8765). This uses the same client database as
`wealth-agent`. Live web search is enabled by default in both launchers, using
[Codex web search](https://learn.chatgpt.com/docs/web-search?surface=cli).
The page shows working status, source links, retryable errors, and a Low/Medium/High
reasoning selector. Low is the default; the chosen level applies to the next message. Client facts
persist across restarts; the visible transcript lasts for the server session.
The launcher also sets low response verbosity, independently of reasoning effort.
Ask for a detailed explanation when you want one; this is not a word limit.
The shared policy in `wealth/instructions.md` replaces the launcher’s built-in
coding instructions. Inherited AGENTS files are disabled for this financial
assistant. Standalone Wealth MCP reads the same policy; the launcher omits the
duplicate MCP copy. Personal context and the request remain in the turn prompt.
The server binds only to localhost and validates request origins and a session
token. It is a local personal interface, not a hosted multi-user application.

Python tools perform correlations, factor regressions, historical risk, stress,
construction and simulations. The model has access to those implemented
calculations, live market-data fetching and web research; it does not have an
unrestricted Python shell. Paste holdings or a question into chat. File uploads
are not implemented in this small browser interface.

## Start in the terminal

Install Codex CLI if it is not already on your PATH, then use its normal login:

```sh
codex login
uv sync
uv run wealth-agent --demo --model sol
```

`codex login status` reports the active authentication method. ChatGPT login
uses your available Codex subscription usage. You do not need to provide an
OpenRouter key for this path. The launcher does not read, copy, or reinterpret
OAuth tokens; the installed Codex runtime handles authentication.

For Luna:

```sh
uv run wealth-agent --demo --model luna
```

Try these messages in sequence:

1. How much can I invest while protecting my home goal and emergency reserve?
2. My home goal is now $100,000. How does that change the plan?
3. What changed, and what do you remember about my priorities?

The fictional demo starts with $300,000 total capital: $200,000 in SPY and
$100,000 cash. The $24,000 reserve and $50,000 home goal leave a $226,000 total
investment budget, including the existing investments, or $26,000 additional
cash to invest. Values are fictional; detailed fund constituents and tax lots
are intentionally missing. Its Wealth memory persists, so restarting the launcher
does not reset your corrections. Sol and Luna can use the same demo memory.
Use `--help` for database/client controls and a one-shot `--prompt` option.

## Start your own profile

```sh
uv run wealth-agent --client my-profile --model sol
```

A new identifier creates an empty local profile; the same identifier resumes it.
An empty interactive profile immediately gets a brief introduction and its first
onboarding question, before you type. A greeting also starts onboarding in
one-shot mode. Profiles with saved facts resume without the first-time introduction.
You can instead start with a specific question. The assistant saves relevant facts automatically and
asks only for missing details needed for that question. You can say not to save
a detail. It distinguishes real facts from hypothetical scenarios. No separate
setup questionnaire or “remember this” command is required.

## What is instantiated

```text
Your terminal → Codex CLI (Sol or Luna, your login)
              → Wealth MCP → local client memory + financial calculations
```

Each turn is an ephemeral Codex execution with bounded recent conversation
context. Durable financial memory lives in SQLite. The launcher displays tool
names separately from the agent's response. Failed model runs report errors.
Only the Wealth server is explicitly configured; global user configuration is
not changed. The agent has no shell tool and uses a read-only execution sandbox.
Wealth tools automatically save relevant explicit facts and corrections in their
configured database; decisions require the user’s actual choice. This is a local trusted-user testing interface, not a
public multi-user application.

No background monitor starts and no trades or transfers are possible through
Wealth. Prompts and selected tool results reach the model provider under the
Codex account's settings. Do not put credentials in a chat or repository.

## SDK versus direct API

This small Python launcher uses the supported `codex exec` protocol. A Node or
Python application can instead use the official Codex SDK. Both approaches
keep authentication inside the Codex runtime; a ChatGPT OAuth token is not used
as an OpenRouter or general OpenAI API key.

For an independent hosted assistant, configure a server-side API provider such
as OpenRouter and attach Wealth as MCP. That is a separate integration from
this local ChatGPT-authenticated launcher.

Official references:
[authentication](https://learn.chatgpt.com/docs/auth),
[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk),
[models](https://learn.chatgpt.com/docs/models).

## Prompt delivery

Each instance serves one person; the profile ID is an internal tool argument.
The launcher supplies `wealth/instructions.md` through `model_instructions_file`,
replacing Codex’s coding instructions. `project_doc_max_bytes=0` prevents inherited
AGENTS instructions. It sets `WEALTH_BEHAVIOR_IN_HOST=1` on its MCP subprocess
to omit the duplicate contract. These overrides preserve the existing Codex login.
A standalone MCP server still supplies that contract by default.

Task schemas come from `wealth_context(intent=task)` without a profile ID.
Personal recall uses the same tool with the profile ID. Conversation and current
request sections are explicitly delimited; financial and memory behavior is
otherwise unchanged.
