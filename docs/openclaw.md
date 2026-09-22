# Wealth in OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) (formerly Clawdbot and Moltbot)
is an open-source personal assistant you run yourself and talk to over WhatsApp,
Telegram, iMessage, Signal or WebChat. This page adds Wealth to it: your claw
gets the Wealth skill, the Wealth MCP server and a private local database, and
can then onboard you, read your statements and answer money questions in the
chat you already use.

Targets OpenClaw 2026.9.x (checked against v2026.9.5 and the docs at
docs.openclaw.ai in September 2026). References are listed at the end.

## Two-minute setup

You need a working OpenClaw gateway with a channel connected, this repository
cloned, and [uv](https://docs.astral.sh/uv/) (`brew install uv`). uv provides
Python 3.11 or newer.

```sh
cd /path/to/wealth-harness
integrations/openclaw/install.sh --dry-run   # shows every change, touches nothing
integrations/openclaw/install.sh
openclaw gateway restart
```

Then message your claw: *hola, quiero ordenar mis finanzas*.

The installer:

1. checks for `uv` (and warns when `openclaw` is not on PATH);
2. runs `uv sync --locked --inexact --extra images` (Pillow, for PNG views), the only
   network step, and checks Python ≥ 3.11;
3. creates a private data directory (mode 0700) at
   `${WEALTH_DATA_DIR:-~/.local/share/wealth-harness}` with the database
   `clients.sqlite3`, `uploads/me/` for statements and `views/` for images,
   and creates the profile `me`;
4. copies the skill to `~/.openclaw/skills/wealth` (`--workspace DIR` installs
   it to `DIR/skills` instead; `--link` symlinks it so `git pull` updates it);
5. backs up `openclaw.json`, then sets `mcp.servers.wealth` and
   `skills.entries.wealth.env`, leaving every other entry alone. With the
   `openclaw` CLI on PATH it uses `openclaw mcp set` and `openclaw config set`;
   without it, it edits the file directly when it is strict JSON, and otherwise
   prints the snippet below for you to paste.

At a terminal it also asks for the name and e-mail the SEC requires of anyone
fetching 13F filings (the manager tasks, "copy this fund manager"), saved as
`WEALTH_SEC_USER_AGENT` in the server's env; press Enter to skip. Without a
terminal it never asks: pass `--sec-user-agent "Your Name you@example.com"` (or
set `WEALTH_SEC_USER_AGENT`) instead. Without it the manager tasks say how to
add it and never call EDGAR.

Rerunning is safe. `--uninstall` removes the skill and both config entries and
keeps your data. `--no-mcp` skips the server and leaves the claw on the CLI.
`OPENCLAW_STATE_DIR` and `OPENCLAW_CONFIG_PATH` are honored.

### Manual configuration

What the installer writes, if you prefer to edit `~/.openclaw/openclaw.json`
yourself (JSON5; use absolute paths):

```json5
{
  mcp: {
    servers: {
      wealth: {
        command: "uv",
        args: ["--directory", "/path/to/wealth-harness", "run", "wealth-mcp"],
        env: {
          WEALTH_DB: "/Users/you/.local/share/wealth-harness/clients.sqlite3",
          WEALTH_UPLOAD_DIR: "/Users/you/.local/share/wealth-harness/uploads",
        },
      },
    },
  },
  skills: {
    entries: {
      wealth: {
        enabled: true,
        env: {
          WEALTH_HOME: "/path/to/wealth-harness",
          WEALTH_DB: "/Users/you/.local/share/wealth-harness/clients.sqlite3",
          WEALTH_UPLOAD_DIR: "/Users/you/.local/share/wealth-harness/uploads",
          WEALTH_VIEW_DIR: "/Users/you/.local/share/wealth-harness/views",
        },
      },
    },
  },
}
```

The CLI equivalent is
`openclaw mcp set wealth '{"command":"uv","args":["--directory","/path/to/wealth-harness","run","wealth-mcp"],"env":{...}}'`.
Copy `integrations/openclaw/skills/wealth` into `~/.openclaw/skills/`.

## How the claw uses Wealth

The skill ([`SKILL.md`](../integrations/openclaw/skills/wealth/SKILL.md)) is a
condensed version of [the conversation policy](../wealth/instructions.md)
written for chat apps. The claw's own model does the talking; Wealth does the
remembering, calculating and drawing.

- **MCP tools** (`wealth_context`, `wealth_run`, `wealth_remember`,
  `wealth_ingest`, `wealth_decision`, …) for everything the web chat can do:
  spending, surplus and savings, portfolio exposure and performance, debt
  payoff, projections, US and Mexico tax scenarios, statements.
- **Shell helpers** (host exec) for what a text channel needs:

| Command | Prints |
| --- | --- |
| `wealth onboarding next --client me [--lang es] [--json]` | The next setup question as numbered text |
| `wealth onboarding answer --client me --step S --text '…' [--json]` | Saves a typed reply (numbers, amounts like "45 mil", "no sé", "omitir") and prints the next question |
| `wealth onboarding answer --client me --step S --answer '<json>'` | Saves an answer the model interpreted |
| `wealth view --client me --task T --input '<json>' --png FILE` | Draws the result's views (at most two) and prints a title and `MEDIA:<path>` per image |
| `wealth today --client me` | Today's nudges as short lines, when this Wealth has `wealth/proactive.py` |

Run them as `uv --directory "$WEALTH_HOME" run wealth …`. Exit codes: 0 done,
2 invalid input, 3 the reply needs the model to interpret it (stdout is a JSON
hint with the step's fields), 4 nothing to send. `--json` output has `text` (the
message to send), `step` (to pass to the next `answer`) and `complete`.

Views are PNGs when Pillow is installed (the `images` extra, which the
installer adds); otherwise the helper writes SVG and says so, and most chat apps
will not preview it. The claw sends the image with its message tool.

## Example: WhatsApp, es-MX

> **Ana:** hola, quiero ordenar mis finanzas
>
> **Claw:** (1/9) ¿Cómo te llamo y dónde vives?
> 1. México
> 2. Estados Unidos
> 3. Otro país
> Escribe tu nombre y el número de dónde vives (por ejemplo, Ana, 1).
>
> **Ana:** Ana, 1
>
> **Claw:** Ana, en México
> (2/9) ¿En qué año naciste? ¿Alguien depende de ti? …
>
> *(income 85 mil, spending como 45 mil)*
>
> **Claw:** Gastas $45,000 al mes
> Te quedan $40,000 al mes
> (5/9) ¿Dónde tienes tu dinero hoy?
> 1. Nu / banco
> 2. CETES
> 3. GBM / casa de bolsa …
>
> **Ana:** 1: 60 mil, 3: 200 mil
>
> **Claw:** Nu / banco $60,000 · GBM / casa de bolsa $200,000
> Patrimonio neto $260,000 · te quedan $40,000 al mes · reserva de 1.3 meses
> (6/9) ¿Debes algo? …
>
> *(after the last question)*
>
> **Claw:** Tu reserva cubre poco más de un mes de gastos, y eso es lo primero:
> con los $40,000 que te quedan al mes llegas a tres meses en unos dos
> meses. Tus $200,000 en GBM pueden seguir invertidos mientras tanto.
> ¿Tienes algún pago grande en los próximos meses?
> *[image: Dónde estás hoy]*

## Example: Telegram, en

> **Sam:** Can I afford to put $1,500 a month into my brokerage?
>
> **Claw:** Yes, with room to spare: after your $4,500 of spending you keep
> about $3,000 a month, and your reserve already covers 2.7 months. $1,500 a
> month leaves $1,500 a month, enough to bring the reserve to six months in
> about ten.
> *[image: A typical month]*
> Is the $10,000 bonus in March reliable enough to count on?
>
> **Sam:** I sold my Tesla shares yesterday
>
> **Claw:** Got it. Do you have the sale price? The gain goes on your 2026
> return, and whether it's short or long term changes the rate.

The claw never says it bought, sold or moved anything, and asks "¿Lo guardo?"
/ "Save it?" before saving a statement.

## Privacy

- Your financial data stays on this machine, in the data directory above
  (0700) and the database (0600). Images from `wealth view` are written 0600.
- The claw's model provider sees the conversation: what you type, what the
  claw answers, and the tool results it reads (figures, not account numbers).
  Pick a provider whose data terms you accept.
- Wealth refuses to store government IDs (RFC, CURP, SSN), account or card
  numbers, addresses and credentials; the skill tells the claw never to echo
  them.
- The helpers print no database path, data directory or home directory, and
  never echo what you typed back in an error.
- Deleting a profile is interactive only:
  `uv run wealth forget --client me` in a terminal. `install.sh --uninstall`
  keeps the data.
- Each turn fetches prices for held symbols from Yahoo Finance (symbols only,
  no amounts or identity); `WEALTH_OFFLINE=1` turns that off. The local web
  page loads fonts from Google Fonts, which sees the IP and the font request
  but no financial data.
- OpenClaw is a host without the Wealth launcher, so it gets `needs_person`
  plus a `confirmation_code`; the claw must show the summary and code, and
  send the second call (`confirm: true` plus the code) only on the person's
  yes. Pending codes live in the database for 10 minutes (single use; a wrong
  code cancels it), so a gateway that starts `wealth-mcp` for every message
  can still finish the second call.
- `WEALTH_HOST_HANDLES_CONSENT=1` (in `mcp.servers.wealth.env`) drops the
  code, for a setup where OpenClaw's own tool approval is the consent: the
  person approves each `wealth_ingest` confirm, `wealth_decision` accept and
  `wealth_resolve_contradiction` call themselves. The trade-off: the code
  proves the yes came from the person after seeing the summary; with the flag,
  a model that calls the tool on its own (say, after reading instructions
  planted in a statement) is stopped only by that approval step. Leave it off
  when Wealth's tools are auto-approved, which is the usual claw setup. The
  installer never sets it. When it edits `openclaw.json` itself a rerun keeps
  it (and `WEALTH_SEC_USER_AGENT`); `openclaw mcp set` replaces the whole
  entry, so after a rerun with the CLI set it again.

## Permissions

Wealth's MCP tools go through OpenClaw's normal tool policy; nothing here
bypasses it (`mcp.servers.wealth.toolFilter` can narrow the tools). The shell
helpers use the exec tool on the gateway host, where the skill's `env` is
injected; a sandboxed exec cannot see the database. In exec `allowlist` mode,
approve `uv` the first time the claw asks (or add it to the exec approvals
allowlist); each helper is a single command with no pipes, so it matches a
plain allowlist entry. Statements arrive as media paths and are copied into
`$WEALTH_UPLOAD_DIR/me/` with `cp` before `wealth_ingest` reads them.

## Troubleshooting

- **The skill does not load.** `openclaw skills list` shows why: `uv` missing
  from the gateway's PATH, or `WEALTH_HOME`/`WEALTH_DB` unset (rerun the
  installer, then `openclaw gateway restart`).
- **No `wealth_*` tools.** `openclaw mcp doctor wealth --probe`. The first
  start runs `uv` and can take a few seconds; an old checkout path in
  `mcp.servers.wealth.args` is the usual cause after moving the repository.
- **"is not strict JSON".** Your `openclaw.json` has comments or trailing
  commas and `openclaw` was not on PATH; paste the printed snippet by hand or
  install the CLI and rerun.
- **Images arrive as files or not at all.** Check that Pillow is installed
  (`uv run python -c "import PIL"`); with `tools.fs.workspaceOnly=true`, point
  `--png` into the agent workspace. Some OpenClaw versions deliver a
  `MEDIA:<path>` line in the reply only through the message tool.
- **Exec asks for approval every time.** Choose "always allow" for `uv`, or
  add it to the allowlist.
- **`wealth today` says nudges are not available.** This Wealth version has
  no `wealth/proactive.py`; the scheduled job then sends nothing.

## References

- Skills, SKILL.md format, locations and gating: https://docs.openclaw.ai/tools/skills
- MCP servers (`mcp.servers`): https://docs.openclaw.ai/tools/mcp and
  https://docs.openclaw.ai/cli/mcp/registry
- Config CLI (`openclaw config set`, JSON5, `OPENCLAW_CONFIG_PATH`): https://docs.openclaw.ai/cli/config
- Exec tool and approvals: https://docs.openclaw.ai/tools/exec
- Media and attachments: https://docs.openclaw.ai/help/faq/media-and-attachments and
  https://docs.openclaw.ai/nodes/images
- Plugins (not needed for Wealth): https://docs.openclaw.ai/tools/plugin
- Releases: https://github.com/openclaw/openclaw/releases
