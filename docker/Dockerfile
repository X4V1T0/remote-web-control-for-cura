# UltiMaker Cura + RemoteWebControl in a container, for an always-on server.
#
# Cura cannot run with --headless (no build volume, no scene: see docs/DESIGN.md, "Running
# without a display"), so it runs its normal GUI on a virtual display (Xvfb) with software OpenGL
# (Mesa llvmpipe). Optional noVNC gives a browser view of that display for maintenance.
#
# Only linux/amd64: UltiMaker publishes no Linux ARM build of Cura.

FROM ubuntu:24.04

ARG CURA_VERSION=5.13.0
ARG CURA_SERIES=5.13

ENV DEBIAN_FRONTEND=noninteractive

ARG NOVNC_VERSION=1.5.0
ARG WEBSOCKIFY_VERSION=0.12.0

# noVNC and websockify come from their GitHub releases instead of Ubuntu's packages, which pull in
# Node.js, ICU, numpy/LAPACK, Babel... (~170 MB) that a static web client and a pure Python proxy
# do not need. websockify only uses numpy, when present, to go faster.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini xvfb x11vnc python3 \
        libgl1 libegl1 libopengl0 libgl1-mesa-dri libglx-mesa0 libgbm1 \
        libxkbcommon0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
        libxcb-render-util0 libxcb-xinerama0 libxcb-shape0 libxcb-cursor0 libxcb-xkb1 libxcb-xfixes0 \
        libx11-xcb1 libxrender1 libxi6 libxext6 libsm6 libice6 libxcomposite1 libxdamage1 libxrandr2 \
        libfontconfig1 libfreetype6 libdbus-1-3 libglib2.0-0t64 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/novnc /opt/websockify \
    && curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
        | tar -xz -C /opt/novnc --strip-components=1 \
    && curl -fsSL "https://github.com/novnc/websockify/archive/refs/tags/v${WEBSOCKIFY_VERSION}.tar.gz" \
        | tar -xz -C /opt/websockify --strip-components=1 \
    && rm -rf /opt/novnc/tests /opt/novnc/docs /opt/websockify/tests /opt/websockify/docs

# Official AppImage, extracted (AppImages need FUSE to run in place, which containers lack).
RUN curl -fsSL -o /tmp/cura.AppImage \
        "https://github.com/Ultimaker/Cura/releases/download/${CURA_VERSION}/UltiMaker-Cura-${CURA_VERSION}-linux-X64.AppImage" \
    && chmod +x /tmp/cura.AppImage \
    && cd /opt && /tmp/cura.AppImage --appimage-extract > /dev/null \
    && mv /opt/squashfs-root /opt/cura \
    && rm /tmp/cura.AppImage \
    # Not used at run time: documentation, message catalogues of the bundled system libraries
    # (Cura's own translations are in share/cura/resources/i18n) and Perl modules (~80 MB).
    && rm -rf /opt/cura/usr/share/doc /opt/cura/usr/share/locale /opt/cura/usr/share/perl

# Unprivileged user that runs Cura. Its two Cura folders are the volumes.
RUN useradd --create-home --uid 1000 --shell /bin/sh cura 2>/dev/null \
        || (userdel --remove ubuntu && useradd --create-home --uid 1000 --shell /bin/sh cura) \
    && mkdir -p /home/cura/.config/cura /home/cura/.local/share/cura \
    && chown -R cura:cura /home/cura \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix  # Xvfb runs as "cura" and cannot create it.

COPY RemoteWebControl /opt/remote-web-control/RemoteWebControl
COPY docker/entrypoint.sh docker/prune_resources.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/prune_resources.sh \
    && find /opt/remote-web-control -name "__pycache__" -prune -exec rm -rf {} +

ENV CURA_SERIES=${CURA_SERIES} \
    LANG=C.UTF-8 \
    DISPLAY=:99 \
    QT_QPA_PLATFORM=xcb \
    LIBGL_ALWAYS_SOFTWARE=1 \
    SCREEN_SIZE=1600x1000x24

# RemoteWebControl (API + app) and noVNC (only when VNC_PASSWORD is set).
EXPOSE 8765 6080

VOLUME ["/home/cura/.config/cura", "/home/cura/.local/share/cura"]

# /api/printers goes through Cura's main thread, so this also detects a hung Cura.
HEALTHCHECK --interval=60s --timeout=40s --start-period=180s --retries=3 \
    CMD curl -fs -o /dev/null -H "Authorization: Bearer $(cat /home/cura/.local/share/cura/${CURA_SERIES}/RemoteWebControl/token.txt)" \
        http://127.0.0.1:8765/api/printers || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
