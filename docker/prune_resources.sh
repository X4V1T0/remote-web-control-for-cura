#!/bin/sh
# Removes Cura's bundled resources for printer brands that are not in use, to lower Cura's memory
# (it loads the metadata of every definition, quality, variant, intent and material at start-up).
#
#   prune_resources.sh "<vendor> <vendor> ..." <cura data dir>
#
# Keeps: the given vendors (file/folder name prefixes, e.g. "creality custom"), the common base
# (fdmprinter, fdmextruder, generic quality files, intents.json), the generic materials and every
# container referenced by the user's printers and extruders. If any referenced container would
# disappear it removes NOTHING (Cura would flag that printer as broken with a modal dialog).
# Only the container's copy of the image is modified; a new container starts from a full copy.
set -eu

KEEP_VENDORS="$1"
DATA_DIR="$2"
R=/opt/cura/share/cura/resources
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

keep_vendor() {
    for vendor in $KEEP_VENDORS; do
        case "$1" in "$vendor"|"$vendor"_*|"$vendor".*) return 0 ;; esac
    done
    return 1
}

# Containers referenced by the user's stacks, e.g. "generic_pla_175", "creality_ender3pro_0.4".
# Ids with spaces or "#" belong to the user's own files, which are not touched.
cat "$DATA_DIR"/machine_instances/*.cfg "$DATA_DIR"/extruders/*.cfg 2>/dev/null \
    | sed -n 's/^[0-9][0-9]* = //p' | grep -v '[ #]' | grep -v '^empty' | sort -u > "$WORK/refs" || true

: > "$WORK/remove"
for f in "$R"/definitions/* "$R"/extruders/*; do
    name=${f##*/}
    case "$name" in fdmprinter.*|fdmextruder.*) continue ;; esac
    keep_vendor "$name" || echo "$f" >> "$WORK/remove"
done
for dir in quality variants intent; do
    for entry in "$R/$dir"/*; do
        name=${entry##*/}
        if [ -d "$entry" ]; then
            keep_vendor "$name" || echo "$entry" >> "$WORK/remove"
        elif [ "$dir" = variants ]; then
            keep_vendor "$name" || echo "$entry" >> "$WORK/remove"   # Some vendors have variant files at the top level.
        fi
    done
done
for f in "$R"/materials/*; do
    base=${f##*/}
    base=${base%%.*}
    case "$base" in generic_*) continue ;; esac
    grep -qx -e "$base" -e "${base}_.*" "$WORK/refs" && continue   # A material (or one of its variants) in use.
    echo "$f" >> "$WORK/remove"
done

# Safety check: every referenced container must still exist after the removal.
missing=""
while read -r id; do
    found=$(find "$R" -name "$id.*" 2>/dev/null | head -n 1)
    [ -z "$found" ] && continue   # Not a bundled container (user file, or already pruned and in the user folder).
    kept=""
    for path in $(find "$R" -name "$id.*" 2>/dev/null); do
        removed=""
        while read -r gone; do
            case "$path" in "$gone"|"$gone"/*) removed=1; break ;; esac
        done < "$WORK/remove"
        [ -z "$removed" ] && kept=1 && break
    done
    [ -z "$kept" ] && missing="$missing $id"
done < "$WORK/refs"

if [ -n "$missing" ]; then
    echo "[RemoteWebControl] Not pruning Cura resources: your printers use containers outside CURA_KEEP_VENDORS ($KEEP_VENDORS):$missing" >&2
    exit 0
fi

count=$(wc -l < "$WORK/remove")
while read -r path; do rm -rf "$path"; done < "$WORK/remove"
echo "[RemoteWebControl] Pruned $count Cura resources outside: $KEEP_VENDORS"
