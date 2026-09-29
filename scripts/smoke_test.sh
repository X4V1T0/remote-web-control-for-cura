#!/usr/bin/env bash
# End-to-end smoke test of RemoteWebControl against a running Cura (on this PC or in Docker).
#
# Usage:
#   scripts/smoke_test.sh
#   RWC_URL=http://192.168.1.10:8765 RWC_TOKEN=... PRINTER_ID="My printer" scripts/smoke_test.sh
#
# Variables (all optional):
#   RWC_URL         Default: http://127.0.0.1:8765
#   RWC_TOKEN       API token. By default it is read from RWC_TOKEN_FILE, or from the
#                           token file of this computer's Cura (CURA_SERIES, default 5.13), or from
#                           ./cura-data (Docker).
#   PRINTER_ID              Printer to use. Default: the first one.
#   STL                     Model to upload. Default: samples/l_bracket.stl
#   INFILL                  infill_sparse_density to set before slicing. Default: 37
#   GCODE_OUT               Where to save the G-code. Default: a temporary file.
#   KEEP_JOB=1              Do not delete the job at the end.
#
# Only needs bash + curl (Git Bash on Windows is fine).
#
# Covers: health -> printers -> profiles -> upload STL -> mesh -> rotate 90 deg in X -> auto-orient
#         -> settings -> change infill_sparse_density -> slice -> wait -> download G-code (checking
#         the setting) -> list -> delete.

set -euo pipefail

URL="${RWC_URL:-http://127.0.0.1:8765}"
SERIES="${CURA_SERIES:-5.13}"

# Token files that may exist: a local Cura and/or a Docker setup. The first one the server
# accepts is used, so a PC with both works with either URL.
find_token() {
    local candidates=(
        "${RWC_TOKEN_FILE:-}"
        "${APPDATA:-$HOME/AppData/Roaming}/cura/$SERIES/RemoteWebControl/token.txt"                # Windows
        "$HOME/Library/Application Support/cura/$SERIES/RemoteWebControl/token.txt"               # macOS
        "${XDG_DATA_HOME:-$HOME/.local/share}/cura/$SERIES/RemoteWebControl/token.txt"             # Linux
        "$(dirname "$0")/../cura-data/data/$SERIES/RemoteWebControl/token.txt"                      # Docker
    )
    local file token
    for file in "${candidates[@]}"; do
        [[ -n "$file" && -s "$file" ]] || continue
        token="$(tr -d '\r\n' < "$file")"
        if [[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" "$URL/api/health")" == "200" ]]; then
            echo "$token"
            return 0
        fi
    done
    return 1
}

if [[ -z "${RWC_TOKEN:-}" ]]; then
    if ! RWC_TOKEN="$(find_token)"; then
        echo "No RWC_TOKEN given, and no RemoteWebControl token file (Cura $SERIES) is accepted by $URL." >&2
        exit 1
    fi
fi

step() { printf '\n=== %s\n' "$*"; }

# api METHOD PATH [curl args...] -> prints body, fails on HTTP >= 400.
api() {
    local method="$1" path="$2"
    shift 2
    local body status
    body="$(mktemp)"
    status="$(curl -sS --compressed -o "$body" -w '%{http_code}' -X "$method" \
        -H "Authorization: Bearer $RWC_TOKEN" "$@" "$URL$path")"
    cat "$body"
    rm -f "$body"
    echo
    if (( status >= 400 )); then
        echo "FAILED: $method $path -> HTTP $status" >&2
        exit 1
    fi
}

step "Unauthenticated request must be rejected"
status="$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/health")"
[[ "$status" == "401" ]] || { echo "Expected 401, got $status" >&2; exit 1; }
echo "OK (401)"

step "GET /api/health"
api GET /api/health

step "GET /api/printers"
printers="$(api GET /api/printers)"
echo "$printers"

PRINTER_ID="${PRINTER_ID:-$(printf '%s' "$printers" | grep -o '"id": "[^"]*"' | head -1 | sed 's/"id": "\(.*\)"/\1/')}"
if [[ -z "$PRINTER_ID" ]]; then
    echo "No printers configured in Cura." >&2
    exit 1
fi
echo "Using printer: $PRINTER_ID"

step "GET /api/printers/{id}/profiles"
# Printer ids are the names given in Cura and may contain spaces or '#'.
api GET "/api/printers/$(printf '%s' "$PRINTER_ID" | sed -e 's/%/%25/g' -e 's/ /%20/g' -e 's/#/%23/g')/profiles"

STL="${STL:-$(dirname "$0")/../samples/l_bracket.stl}"
step "POST /api/jobs ($STL, printer's current profile)"
job="$(api POST /api/jobs -F "file=@$STL" -F "printer_id=$PRINTER_ID")"
echo "$job"
JOB_ID="$(printf '%s' "$job" | grep -o '"id": "[0-9a-f]\{32\}"' | head -1 | sed 's/"id": "\(.*\)"/\1/')"
[[ -n "$JOB_ID" ]] || { echo "No job id in the response" >&2; exit 1; }
echo "Job: $JOB_ID"

step "GET /api/jobs/{id}/mesh (binary CRM1)"
MESH_OUT="${TMPDIR:-/tmp}/rwc_smoke.mesh"
curl -sS --compressed -f -H "Authorization: Bearer $RWC_TOKEN" -o "$MESH_OUT" "$URL/api/jobs/$JOB_ID/mesh"
magic="$(head -c 4 "$MESH_OUT")"
rm -f "$MESH_OUT"
[[ "$magic" == "CRM1" ]] || { echo "Unexpected mesh magic: $magic" >&2; exit 1; }
echo "OK (CRM1)"

step "PUT /api/jobs/{id}/transform (rotate 90 deg around X)"
placement="$(api PUT "/api/jobs/$JOB_ID/transform" -H "Content-Type: application/json" \
    -d '{"matrix": [1,0,0,0, 0,0,-1,0, 0,1,0,0, 0,0,0,1]}')"
echo "$placement"
printf '%s' "$placement" | grep -q '"fits": true' || echo "WARNING: the model does not fit (see warnings)"

step "POST /api/jobs/{id}/auto-orient"
auto_status="$(curl -sS -o "${TMPDIR:-/tmp}/rwc_auto.json" -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $RWC_TOKEN" "$URL/api/jobs/$JOB_ID/auto-orient")"
cat "${TMPDIR:-/tmp}/rwc_auto.json"; echo
rm -f "${TMPDIR:-/tmp}/rwc_auto.json"
if [[ "$auto_status" == "422" ]]; then
    echo "WARNING: auto-orientation unavailable (install the 'Auto Orientation' plugin in Cura)"
elif [[ "$auto_status" != "200" ]]; then
    echo "FAILED: auto-orient -> HTTP $auto_status" >&2
    exit 1
fi

step "GET /api/jobs/{id}/settings (basic, es_ES)"
settings="$(api GET "/api/jobs/$JOB_ID/settings?visibility=basic&lang=es_ES")"
printf '%s\n' "$settings" | grep -o '"key": "infill_sparse_density", "label": "[^"]*"' | head -1
printf '%s\n' "$settings" | grep -o '"key": "infill_sparse_density"[^{]*"value": [0-9.]*' | grep -o '"value": [0-9.]*' | head -1

INFILL="${INFILL:-37}"
step "PATCH /api/jobs/{id}/settings (infill_sparse_density = $INFILL on extruder 0)"
patch="$(api PATCH "/api/jobs/$JOB_ID/settings" -H "Content-Type: application/json" \
    -d "{\"scope\": \"extruder\", \"extruder\": 0, \"key\": \"infill_sparse_density\", \"value\": $INFILL}")"
printf '%s\n' "$patch" | cut -c1-800
printf '%s' "$patch" | grep -q '"key": "infill_line_distance"' || echo "WARNING: infill_line_distance did not change"

step "POST /api/jobs/{id}/slice"
api POST "/api/jobs/$JOB_ID/slice"

step "Waiting for the slice"
state=""
for _ in $(seq 1 600); do
    job="$(api GET "/api/jobs/$JOB_ID")"
    state="$(printf '%s' "$job" | grep -o '"state": "[a-z]*"' | head -1 | cut -d'"' -f4)"
    progress="$(printf '%s' "$job" | grep -o '"progress": [0-9.]*' | head -1 | cut -d' ' -f2)"
    printf '\r  %-8s %s   ' "$state" "$progress"
    [[ "$state" == "done" || "$state" == "error" ]] && break
    sleep 1
done
echo
if [[ "$state" != "done" ]]; then
    echo "$job"
    echo "FAILED: the job ended in state '$state'" >&2
    exit 1
fi
printf '%s\n' "$job" | grep -o '"result": {.*}' | cut -c1-600

step "GET /api/jobs/{id}/gcode"
OUT="${GCODE_OUT:-${TMPDIR:-/tmp}/rwc_smoke.gcode}"
curl -sS --compressed -f -H "Authorization: Bearer $RWC_TOKEN" -o "$OUT" -D - "$URL/api/jobs/$JOB_ID/gcode" \
    | grep -i "^content-disposition\|^content-encoding\|^content-length"
lines="$(wc -l < "$OUT")"
echo "Saved $OUT ($lines lines)"
grep -q "^G1 " "$OUT" || { echo "FAILED: no G1 moves in the G-code" >&2; exit 1; }
# The settings Cura used are serialised at the end of the file (;SETTING_3 lines, escaped).
if tr -d '\r\n' < "$OUT" | sed 's/;SETTING_3 //g' | grep -q "infill_sparse_density = $INFILL"; then
    echo "OK: the G-code was sliced with infill_sparse_density = $INFILL"
else
    echo "FAILED: infill_sparse_density = $INFILL not found in the serialised settings" >&2
    exit 1
fi
head -3 "$OUT"

step "GET /api/jobs"
api GET /api/jobs

if [[ "${KEEP_JOB:-0}" != "1" ]]; then
    step "DELETE /api/jobs/{id}"
    api DELETE "/api/jobs/$JOB_ID"
fi

step "Done"
