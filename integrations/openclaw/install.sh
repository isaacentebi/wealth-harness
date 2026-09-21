#!/bin/sh
# Add Wealth to an OpenClaw assistant: the skill, the MCP server and a private data directory.
#
#   integrations/openclaw/install.sh [--dry-run] [--uninstall] [--link] [--workspace DIR] [--no-mcp]
#
# Idempotent: rerunning updates the skill and the config entries in place. The only
# network step is `uv sync`. The OpenClaw config is backed up before any edit and only
# mcp.servers.wealth and skills.entries.wealth are touched. See docs/openclaw.md.
#
# Environment: OPENCLAW_STATE_DIR (default ~/.openclaw), OPENCLAW_CONFIG_PATH
# (default $OPENCLAW_STATE_DIR/openclaw.json), WEALTH_DATA_DIR (default
# ${XDG_DATA_HOME:-~/.local/share}/wealth-harness), WEALTH_DB (default
# $WEALTH_DATA_DIR/clients.sqlite3).

set -eu
umask 077

DRY_RUN=0
UNINSTALL=0
LINK=0
MCP=1
WORKSPACE=""
CLIENT=me

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --link) LINK=1 ;;
        --no-mcp) MCP=0 ;;
        --workspace)
            [ $# -ge 2 ] || { echo "--workspace needs a directory" >&2; exit 2; }
            WORKSPACE=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
WEALTH_HOME=$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd -P)
SKILL_SRC=$SCRIPT_DIR/skills/wealth
STATE_DIR=${OPENCLAW_STATE_DIR:-$HOME/.openclaw}
CONFIG=${OPENCLAW_CONFIG_PATH:-$STATE_DIR/openclaw.json}
DATA_DIR=${WEALTH_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wealth-harness}
DB=${WEALTH_DB:-$DATA_DIR/clients.sqlite3}
UPLOAD_DIR=$DATA_DIR/uploads
VIEW_DIR=$DATA_DIR/views
if [ -n "$WORKSPACE" ]; then
    SKILLS_DIR=$WORKSPACE/skills
else
    SKILLS_DIR=$STATE_DIR/skills
fi
DEST=$SKILLS_DIR/wealth
MARKER=.installed-by-wealth
STAMP=$(date +%Y%m%d%H%M%S)

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run]'; printf ' %s' "$@"; printf '\n'
    else
        "$@"
    fi
}
have() { command -v "$1" >/dev/null 2>&1; }

python_bin() {
    if [ -x "$WEALTH_HOME/.venv/bin/python" ]; then
        printf '%s\n' "$WEALTH_HOME/.venv/bin/python"
    elif have python3; then
        command -v python3
    else
        return 1
    fi
}

config_tool() {
    PY=$(python_bin) || die "python3 is needed to edit $CONFIG"
    "$PY" "$SCRIPT_DIR/openclaw_config.py" "$@"
}

backup_config() {
    if [ -f "$CONFIG" ]; then
        run cp -p "$CONFIG" "$CONFIG.wealth-backup-$STAMP"
        [ "$DRY_RUN" -eq 1 ] || say "backed up $CONFIG to $(basename "$CONFIG").wealth-backup-$STAMP"
    fi
}

ours() {
    # True when $DEST was put there by this script (a copy with the marker, or a link to this checkout).
    if [ -L "$DEST" ]; then
        [ "$(CDPATH='' cd -- "$DEST" 2>/dev/null && pwd -P)" = "$(CDPATH='' cd -- "$SKILL_SRC" && pwd -P)" ]
    else
        [ -f "$DEST/$MARKER" ]
    fi
}

# ------------------------------------------------------------------ uninstall

if [ "$UNINSTALL" -eq 1 ]; then
    if [ -e "$DEST" ] || [ -L "$DEST" ]; then
        if ours; then
            run rm -rf "$DEST"
            say "removed the skill from $SKILLS_DIR"
        else
            warn "$DEST was not installed by this script; left it alone"
        fi
    fi
    if have openclaw && [ "$DRY_RUN" -eq 0 ]; then
        backup_config
        openclaw mcp unset wealth >/dev/null 2>&1 || true
        openclaw config unset skills.entries.wealth >/dev/null 2>&1 || true
        say "removed mcp.servers.wealth and skills.entries.wealth from the OpenClaw config"
    elif [ -f "$CONFIG" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            say "[dry-run] would back up $CONFIG and remove mcp.servers.wealth and skills.entries.wealth"
        else
            config_tool remove --config "$CONFIG"
        fi
    fi
    say "Your Wealth data was kept in $DATA_DIR."
    say "To delete it, run 'uv run wealth forget --client $CLIENT' from $WEALTH_HOME, or remove that directory yourself."
    exit 0
fi

# ------------------------------------------------------------------ prerequisites

[ -f "$SKILL_SRC/SKILL.md" ] || die "missing $SKILL_SRC/SKILL.md; run this from a Wealth checkout"
[ -f "$WEALTH_HOME/pyproject.toml" ] || die "$WEALTH_HOME does not look like a Wealth checkout"
if ! have uv; then
    die "uv is required (https://docs.astral.sh/uv/): brew install uv, then rerun"
fi
OPENCLAW=0
if have openclaw; then
    OPENCLAW=1
else
    warn "openclaw is not on PATH; the config will be edited directly (strict JSON only)"
fi

say "Wealth checkout:  $WEALTH_HOME"
say "OpenClaw config:  $CONFIG"
say "Skill directory:  $DEST"
say "Private data:     $DATA_DIR"

# ------------------------------------------------------------------ dependencies (the only network step)

# --extra images adds Pillow so views arrive as PNG; --inexact keeps packages a developer installed.
run uv --directory "$WEALTH_HOME" sync --quiet --inexact --extra images
if [ "$DRY_RUN" -eq 0 ]; then
    uv --directory "$WEALTH_HOME" run --quiet python -c \
        'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "Wealth needs Python 3.11 or newer: uv python install 3.12, then rerun"
    uv --directory "$WEALTH_HOME" run --quiet python -c 'import PIL' 2>/dev/null \
        || warn "Pillow is not importable; views will be sent as SVG, which most chat apps do not preview"
fi

# ------------------------------------------------------------------ private data directory and profile

run mkdir -p "$DATA_DIR" "$UPLOAD_DIR/$CLIENT" "$VIEW_DIR" "$(dirname "$DB")"
run chmod 700 "$DATA_DIR" "$UPLOAD_DIR" "$UPLOAD_DIR/$CLIENT" "$VIEW_DIR"
if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] would create the Wealth profile '$CLIENT' in $DB if it does not exist"
else
    if out=$(printf '%s' "{\"action\":\"create\",\"client_id\":\"$CLIENT\",\"inputs\":{\"display_name\":\"$CLIENT\"}}" \
            | WEALTH_DB=$DB uv --directory "$WEALTH_HOME" run --quiet wealth client 2>&1); then
        say "created the Wealth profile"
    else
        case "$out" in
            *"already exists"*) say "the Wealth profile already exists" ;;
            *) die "could not create the Wealth profile: $out" ;;
        esac
    fi
    [ -f "$DB" ] && chmod 600 "$DB"
fi

# ------------------------------------------------------------------ skill

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    if ours; then
        run rm -rf "$DEST"
    else
        run mv "$DEST" "$DEST.backup-$STAMP"
        warn "an existing $DEST was moved to $(basename "$DEST").backup-$STAMP"
    fi
fi
run mkdir -p "$SKILLS_DIR"
if [ "$LINK" -eq 1 ]; then
    run ln -s "$SKILL_SRC" "$DEST"
else
    run cp -R "$SKILL_SRC" "$DEST"
    run touch "$DEST/$MARKER"
fi
say "installed the skill ($([ "$LINK" -eq 1 ] && echo symlink || echo copy))"

# ------------------------------------------------------------------ OpenClaw config

NO_MCP_FLAG=""
[ "$MCP" -eq 1 ] || NO_MCP_FLAG=--no-mcp
if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] would back up $CONFIG and set these entries (all others kept):"
    config_tool snippet --home "$WEALTH_HOME" --db "$DB" --uploads "$UPLOAD_DIR" --views "$VIEW_DIR" $NO_MCP_FLAG
elif [ "$OPENCLAW" -eq 1 ]; then
    backup_config
    JSON=$(config_tool json --home "$WEALTH_HOME" --db "$DB" --uploads "$UPLOAD_DIR" --views "$VIEW_DIR")
    SERVER_JSON=$(printf '%s\n' "$JSON" | sed -n 1p)
    if [ "$MCP" -eq 1 ]; then
        openclaw mcp set wealth "$SERVER_JSON" >/dev/null
        say "registered the MCP server (mcp.servers.wealth)"
    fi
    for name in WEALTH_HOME WEALTH_DB WEALTH_UPLOAD_DIR WEALTH_VIEW_DIR; do
        case "$name" in
            WEALTH_HOME) value=$WEALTH_HOME ;;
            WEALTH_DB) value=$DB ;;
            WEALTH_UPLOAD_DIR) value=$UPLOAD_DIR ;;
            WEALTH_VIEW_DIR) value=$VIEW_DIR ;;
        esac
        quoted=$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')
        openclaw config set "skills.entries.wealth.env.$name" "\"$quoted\"" --strict-json >/dev/null
    done
    say "set skills.entries.wealth.env"
else
    # openclaw_config.py backs the file up itself, and only when something changes.
    config_tool merge --config "$CONFIG" --home "$WEALTH_HOME" --db "$DB" --uploads "$UPLOAD_DIR" \
        --views "$VIEW_DIR" $NO_MCP_FLAG || {
        status=$?
        [ "$status" -eq 3 ] && die "edit $CONFIG by hand with the snippet above, then rerun"
        exit "$status"
    }
fi

# ------------------------------------------------------------------ next step

say ""
say "Next:"
say "  1. Restart the gateway so it loads the skill and server: openclaw gateway restart"
[ "$MCP" -eq 1 ] && say "  2. Check the server: openclaw mcp doctor wealth --probe"
say "  3. Message your assistant, for example: hola, quiero ordenar mis finanzas"
say "If exec runs in allowlist mode, approve 'uv' the first time it asks. See docs/openclaw.md."
