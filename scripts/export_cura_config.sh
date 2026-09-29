#!/usr/bin/env bash
# Exports an UltiMaker Cura configuration (printers, profiles, materials, user plugins and
# post-processing scripts) into the layout that Cura uses on Linux, ready to be mounted by
# docker-compose.yml:
#
#   <dest>/config/<series>   ->  /home/cura/.config/cura/<series>        (cura.cfg, plugins.json)
#   <dest>/data/<series>     ->  /home/cura/.local/share/cura/<series>   (everything else)
#
# Usage:
#   scripts/export_cura_config.sh [--series 5.13] [--source DIR] [--dest DIR] [--new-token]
#
#   --series     Cura series (first two numbers of the version). Default: the newest one found.
#   --source     Cura's configuration folder, when it is not in the usual place:
#                  Windows  %APPDATA%\cura\<series>
#                  macOS    ~/Library/Application Support/cura/<series>
#                  Linux    ~/.local/share/cura/<series> (data) and ~/.config/cura/<series> (config)
#                On Linux, --source is the data folder; the config folder is taken from
#                --config-source or ~/.config/cura/<series>.
#   --dest       Destination. Default: ./cura-data
#   --new-token  Do not export RemoteWebControl's API token: the server generates a new one.
#
# Runs in Git Bash on Windows, and on macOS and Linux. Nothing is modified in the source.

set -euo pipefail

SERIES=""
SOURCE=""
CONFIG_SOURCE=""
DEST="cura-data"
NEW_TOKEN=0

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --series) SERIES="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        --config-source) CONFIG_SOURCE="$2"; shift 2 ;;
        --dest) DEST="$2"; shift 2 ;;
        --new-token) NEW_TOKEN=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done

# Cura's base folder on this system.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) BASE_DATA="${APPDATA:-$HOME/AppData/Roaming}/cura"; BASE_CONFIG="$BASE_DATA" ;;
    Darwin) BASE_DATA="$HOME/Library/Application Support/cura"; BASE_CONFIG="$BASE_DATA" ;;
    *) BASE_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/cura"; BASE_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/cura" ;;
esac

if [[ -z "$SERIES" ]]; then
    if [[ -n "$SOURCE" ]]; then
        SERIES="$(basename "$SOURCE")"
    else
        # Newest "X.Y" folder (sort -V understands 5.9 < 5.13).
        SERIES="$(ls -1 "$BASE_CONFIG" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)"
    fi
fi
if [[ ! "$SERIES" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "Could not determine the Cura series (e.g. 5.13). Use --series or --source." >&2
    exit 1
fi

DATA_SOURCE="${SOURCE:-$BASE_DATA/$SERIES}"
CONFIG_SOURCE="${CONFIG_SOURCE:-$BASE_CONFIG/$SERIES}"
[[ -n "$SOURCE" && "$BASE_DATA" == "$BASE_CONFIG" ]] && CONFIG_SOURCE="$SOURCE"   # Windows/macOS: one folder.

if [[ ! -f "$CONFIG_SOURCE/cura.cfg" ]]; then
    echo "No Cura $SERIES configuration found: '$CONFIG_SOURCE/cura.cfg' does not exist." >&2
    echo "Open that Cura version once, or pass --source / --series." >&2
    exit 1
fi
if [[ ! -d "$DATA_SOURCE/machine_instances" ]]; then
    echo "Warning: '$DATA_SOURCE' has no printers (machine_instances). Exporting anyway." >&2
fi
if [[ -e "$DEST/config/$SERIES" || -e "$DEST/data/$SERIES" ]]; then
    echo "'$DEST' already has a Cura $SERIES configuration. Delete it first to export again" >&2
    echo "(it may contain the server's jobs and token)." >&2
    exit 1
fi

mkdir -p "$DEST/config/$SERIES" "$DEST/data/$SERIES"

# Preferences -> config
for name in cura.cfg plugins.json; do
    [[ -f "$CONFIG_SOURCE/$name" ]] && cp "$CONFIG_SOURCE/$name" "$DEST/config/$SERIES/"
done
if [[ "$NEW_TOKEN" == 1 ]]; then
    # Drop "token = ..." from the [remotewebcontrol] section: the server generates a new token.
    awk '/^\[/{section=$0} !(section=="[remotewebcontrol]" && $1=="token") {print}' \
        "$DEST/config/$SERIES/cura.cfg" > "$DEST/config/$SERIES/cura.cfg.tmp"
    mv "$DEST/config/$SERIES/cura.cfg.tmp" "$DEST/config/$SERIES/cura.cfg"
fi

# Everything else -> data. Left out: logs, backups, caches, preferences (already copied) and
# RemoteWebControl itself (the image provides the plugin; its jobs and token file are per machine).
tar -C "$DATA_SOURCE" \
    --exclude="./cura.cfg" --exclude="./plugins.json" \
    --exclude="./cura.log*" --exclude="./cura *.cfg" --exclude="./*.bak" \
    --exclude="./cache" --exclude="./RemoteWebControl" --exclude="./plugins/RemoteWebControl" \
    --exclude="__pycache__" \
    -cf - . | tar -C "$DEST/data/$SERIES" -xf -

printers=$(ls "$DEST/data/$SERIES/machine_instances" 2>/dev/null | wc -l | tr -d ' ')
plugins=$(ls "$DEST/data/$SERIES/plugins" 2>/dev/null | tr '\n' ' ')
echo "Exported Cura $SERIES from:"
echo "  $CONFIG_SOURCE"
[[ "$DATA_SOURCE" != "$CONFIG_SOURCE" ]] && echo "  $DATA_SOURCE"
echo "to:"
echo "  $DEST/config/$SERIES"
echo "  $DEST/data/$SERIES   ($printers printer(s); user plugins: ${plugins:-none})"
[[ "$NEW_TOKEN" == 1 ]] && echo "The API token was not exported: the server will create a new one."
echo "Next: set CURA_SERIES=$SERIES in .env if it is not 5.13, then: docker compose up -d --build"
