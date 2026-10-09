#!/bin/sh
# Starts the virtual display, the optional noVNC view and Cura with the RemoteWebControl plugin.
set -eu

CONFIG_DIR=/home/cura/.config/cura/$CURA_SERIES
DATA_DIR=/home/cura/.local/share/cura/$CURA_SERIES

if [ "$(id -u)" = "0" ]; then
    # Volumes mounted from the host may belong to another user: give them to "cura" and drop root.
    mkdir -p "$CONFIG_DIR" "$DATA_DIR/plugins" /home/cura/.cache/cura
    chown -R cura:cura /home/cura/.config/cura /home/cura/.local/share/cura /home/cura/.cache/cura
    if [ -n "${CURA_KEEP_VENDORS:-}" ]; then
        prune_resources.sh "$CURA_KEEP_VENDORS" "$DATA_DIR" || echo "[RemoteWebControl] Resource pruning failed; continuing." >&2
    fi
    # The whole environment is kept (compose variables, tuning such as MALLOC_ARENA_MAX).
    exec setpriv --reuid=cura --regid=cura --init-groups \
        env HOME=/home/cura USER=cura LOGNAME=cura "$0" "$@"
fi

mkdir -p "$CONFIG_DIR" "$DATA_DIR/plugins"

# The plugin always comes from the image, so updating the image updates the plugin.
rm -rf "$DATA_DIR/plugins/RemoteWebControl"
ln -s /opt/remote-web-control/RemoteWebControl "$DATA_DIR/plugins/RemoteWebControl"

# A container restart keeps /tmp: a stale lock from the previous run would stop Xvfb from starting.
rm -f "/tmp/.X${DISPLAY#:}-lock" "/tmp/.X11-unix/X${DISPLAY#:}"
Xvfb "$DISPLAY" -screen 0 "$SCREEN_SIZE" -nolisten tcp &
for _ in $(seq 1 50); do
    [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && break
    sleep 0.1
done
if [ ! -e "/tmp/.X11-unix/X${DISPLAY#:}" ]; then
    echo "[RemoteWebControl] Xvfb did not start" >&2
    exit 1
fi

if [ -n "${VNC_PASSWORD:-}" ]; then
    x11vnc -display "$DISPLAY" -forever -shared -quiet -rfbport 5900 -localhost -passwd "$VNC_PASSWORD" > /dev/null 2>&1 &
    PYTHONPATH=/opt/websockify python3 -m websockify --web=/opt/novnc 6080 localhost:5900 > /dev/null 2>&1 &
    echo "[RemoteWebControl] noVNC: http://<server>:6080/vnc.html"
fi

# Print the API token once Cura has started (it is also in $DATA_DIR/RemoteWebControl/token.txt).
(
    for _ in $(seq 1 300); do
        if [ -s "$DATA_DIR/RemoteWebControl/token.txt" ]; then
            echo "[RemoteWebControl] API token: $(cat "$DATA_DIR/RemoteWebControl/token.txt")"
            echo "[RemoteWebControl] Pairing page: http://<server>:8765/pair.html"
            break
        fi
        sleep 1
    done
) &

exec /opt/cura/AppRun "$@"
