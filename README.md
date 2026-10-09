# Remote Web Control for Cura

A plugin for [UltiMaker Cura](https://ultimaker.com/software/ultimaker-cura/) that lets you slice **from your phone**: upload one or more STLs, pick a printer and a profile, place the models in a 3D viewer, change settings, slice and download the G-code.

Cura still does all the work: the plugin uses Cura's own settings stack, scene, slicing engine and post-processing scripts. The **G-code is the same one Cura's GUI would produce** with the same printer, profile, settings and orientation.

There are two ways to use it:

- **Plugin on your PC**: install it in the Cura you already use; your phone connects to that PC.
- **Docker**: Cura and the plugin in a container on an always-on server, with a copy of your Cura configuration.

> **Unofficial.** Remote Web Control for Cura is not affiliated with, sponsored or endorsed by UltiMaker. "UltiMaker" and "Cura" are trademarks of UltiMaker.

> **For your local network only.** The API and the app use plain HTTP, protected by a token. Do not expose them to the internet. See [Security](#security).

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Option A: plugin on your PC](#option-a-plugin-on-your-pc)
- [Option B: Docker](#option-b-docker)
- [Exporting your Cura configuration](#exporting-your-cura-configuration)
- [Pairing your phone](#pairing-your-phone)
- [Plugin configuration](#plugin-configuration)
- [Using the app](#using-the-app)
- [Security](#security)
- [Known limitations](#known-limitations)
- [Development](#development)
- [License and credits](#license-and-credits)

## Features

- **HTTP API** ([reference](docs/API.md)):
  - Cura's printers and profiles;
  - the settings schema, with Cura's Basic/Advanced/Expert visibility and Cura's translations;
  - jobs with one or more models on the same plate (several STLs, copies), with a mesh preview, orientation, auto-orientation and Cura's own arrange;
  - setting changes, returning the diff Cura recalculates;
  - a slicing queue with progress and cancellation;
  - G-code download, with the estimated print time and material.
- **Phone app**: a web app served by the plugin itself. You can add it to your home screen, and it has a 3D viewer for placing the model. Nothing else to install. It is in **English and Spanish** and follows your phone's language, with a dark theme and a light one in Cura's colours.
- **Docker image** with Cura, the plugin and optional noVNC, so you can see Cura's screen from a browser.
- **Script** to bring your Cura configuration (printers, profiles, materials, plugins and scripts) into Docker.

## Requirements

- **UltiMaker Cura 5.13 or later** (plugin SDK 8.12). Tested with 5.13.0.
- **Plugin on your PC**: tested on Windows 11. It should work on macOS and Linux, but that is untested.
- **Docker**: an **x86_64** machine (Intel or AMD) with Docker and Docker Compose. There is no Cura build for Linux ARM, so a Raspberry Pi will not do. About 400–450 MB of RAM and 1.3 GB of disk.
- **Phone**: a current browser. Tested on iPhone (Safari) and Chromium.
- **Optional**: Cura's **Auto Orientation** plugin (from the Marketplace), to auto-orient models.

## Option A: plugin on your PC

1. Download this repository and copy the [`RemoteWebControl/`](RemoteWebControl/) folder into Cura's plugins folder:

   | System | Plugins folder |
   |---|---|
   | Windows | `%APPDATA%\cura\<version>\plugins\` |
   | macOS | `~/Library/Application Support/cura/<version>/plugins/` |
   | Linux | `~/.local/share/cura/<version>/plugins/` |

   `<version>` is the first two numbers of your Cura version (for example `5.13`). You should end up with `plugins/RemoteWebControl/__init__.py`.

2. Restart Cura. **Extensions → Remote Web Control** has two entries:
   - **Pair a phone**: opens the QR code page (see [Pairing your phone](#pairing-your-phone)).
   - **Show URL and token**: shows the addresses of the app and the API.

   The menu follows Cura's language: in a Spanish Cura they are *Emparejar móvil* and *Mostrar URL y token*.

3. The first time, Windows may ask for firewall permission for Cura. Allow it on **private networks** so your phone can reach the PC.

The plugin needs **Cura's build plate to be empty**: for each operation it prepares Cura, slices and then restores it. It works best in a Cura dedicated to this, or one you are not using at that moment.

**For development**, instead of copying the folder you can link it, so you can edit the code without copying it again (restart Cura after each Python change):

```bat
:: Windows (cmd), no administrator rights needed:
mklink /J "%APPDATA%\cura\5.13\plugins\RemoteWebControl" "C:\path\to\the\repository\RemoteWebControl"
```
```sh
# macOS / Linux
ln -s "$PWD/RemoteWebControl" "$HOME/.local/share/cura/5.13/plugins/RemoteWebControl"
```

## Option B: Docker

The image installs the official Cura build for Linux (AppImage) and runs its normal GUI on a virtual display, with software OpenGL. Cura's `--headless` mode cannot be used because it does not create the scene.

### 1. Bring your Cura configuration

On the computer where Cura is already set up, from the repository folder (on Windows, in Git Bash):

```sh
scripts/export_cura_config.sh --new-token
```

This creates the `cura-data/` folder with your printers, profiles, materials, user plugins and post-processing scripts. See [Exporting your Cura configuration](#exporting-your-cura-configuration) for details. If you run the script somewhere other than the server, copy `cura-data/` into the repository folder on the server.

### 2. Configure

```sh
cp .env.example .env
```

Every variable is optional, and the file explains each one. The most useful ones:

| Variable | What for |
|---|---|
| `CURA_KEEP_VENDORS` | Saves ~100 MB of RAM by keeping only the resources of your printer brands, for example `creality custom`. |
| `VNC_PASSWORD` | Turns on noVNC, to see Cura's screen at `http://<server>:6080/vnc.html`. |
| `CURA_VERSION` / `CURA_SERIES` | Another Cura version (default 5.13.0 / 5.13). |
| `RWC_PORT`, `NOVNC_PORT` | Ports on the server (default 8765 and 6080). |
| `RWC_PUBLIC_URL` | The address phones use to reach the app. Only needed if you open the pairing page on the server itself through `localhost`. |

### 3. Start

```sh
docker compose up -d --build
docker compose logs -f        # shows the API token on start-up
docker compose ps             # "healthy" once Cura answers
```

- The app: `http://<server>:8765/`
- To pair your phone, open `http://<server>:8765/pair.html` from any computer and paste the token from the logs **once**. With Docker, that page is not open "on Cura's PC", so it needs the token.

### Maintenance

- **Updating the plugin or the image**: `docker compose up -d --build`. The plugin always comes from the image.
- **Data**: `cura-data/config`, `cura-data/data` and `cura-data/cache` are the volumes. They hold Cura's configuration, the jobs and the token (`cura-data/data/<series>/RemoteWebControl/token.txt`).
- **Marketplace plugins** (such as Auto Orientation): install them through noVNC, then restart Cura (`docker compose restart`). Cura only installs them on the next start, from `cura-data/cache`. Alternatively, install them in Cura on your computer before exporting the configuration.
- **Adding a printer of another brand**: do it through noVNC. If you use `CURA_KEEP_VENDORS`, first add the brand to the list (or empty it) and recreate the container (`docker compose up -d`).
- **Memory**: ~375 MB on start-up with `CURA_KEEP_VENDORS`, ~475 MB without it. It grows a little after a few slices, because Cura keeps caches. If a printer uses anything outside the list, nothing is pruned and the logs say so.
- **Healthcheck**: it queries Cura through its main thread, so it also catches a hung Cura. If a dialog is ever left open in Cura, close it through noVNC.

## Exporting your Cura configuration

`scripts/export_cura_config.sh` copies this computer's Cura configuration into the folder layout Cura uses on Linux, ready for Docker. On Linux, Cura keeps its configuration (`cura.cfg`, `plugins.json`) apart from the rest of its data; on Windows and macOS everything is in one folder, and the script splits it.

```sh
scripts/export_cura_config.sh [--series 5.13] [--source DIR] [--config-source DIR] [--dest DIR] [--new-token]
```

| Option | What it does |
|---|---|
| *(no options)* | Looks for Cura's configuration in your system's usual folder, picks the newest version and exports it to `./cura-data`. |
| `--series 5.13` | Exports that Cura version instead of the newest. |
| `--source DIR` | Cura's configuration folder, if it is not in the usual place. On Linux this is the data folder (`~/.local/share/cura/<series>`). |
| `--config-source DIR` | Linux only: the configuration folder (`~/.config/cura/<series>`), if it is not in the usual place. |
| `--dest DIR` | Destination folder (default `./cura-data`). If you change it, set the same value as `CURA_DATA_DIR` in `.env`. |
| `--new-token` | Does not copy this computer's Remote Web Control token: the server generates its own. Recommended. |

What is copied and what is not:

- **Copied**: printers, extruders, custom quality profiles, materials, variants, setting visibility presets, `packages.json`, user plugins (for example Auto Orientation) and post-processing scripts.
- **Not copied**: logs, backups, caches, and Remote Web Control itself (the image provides it) with its jobs.

The script never changes anything in the source, and it refuses to overwrite a destination that already has that version, so it cannot clobber the server's jobs or token. It works in Git Bash (Windows), macOS and Linux.

## Pairing your phone

1. Open the pairing page:
   - **Plugin on your PC**: Extensions → Remote Web Control → **Pair a phone**.
   - **Docker**: `http://<server>:8765/pair.html`, pasting the token the first time.
2. The page shows a **QR code** and a **6-digit PIN**, valid for 10 minutes.
3. With your phone on the same network, scan the QR code with the camera: the app opens already paired.
4. **iPhone**: in Safari, Share → **Add to Home Screen**. Open the app from the icon and type the PIN. On iOS, the app on the icon keeps its data apart from Safari, so it asks to pair again.
5. **Android**: in Chrome, menu ⋮ → **Add to Home screen**.

PINs can only be generated on the computer where Cura runs, or with the token. A PIN is revoked after 10 failed attempts. You can also paste the token into the app by hand.

## Plugin configuration

The configuration is stored as Cura preferences, in the `[remotewebcontrol]` section of `cura.cfg`:

| System | File |
|---|---|
| Windows | `%APPDATA%\cura\<version>\cura.cfg` |
| macOS | `~/Library/Application Support/cura/<version>/cura.cfg` |
| Linux | `~/.config/cura/<version>/cura.cfg` |
| Docker | `cura-data/config/<version>/cura.cfg` |

You don't need to touch anything: the section is created with the default values the first time. To change something, **close Cura** (or stop the container), edit the file and open Cura again. If Cura is open, it overwrites the file when it closes. An empty or missing key means the default value.

```ini
[remotewebcontrol]
bind_address =
port =
token =
cors_origins =
max_upload_mb =
job_retention_days =
```

| Key | Default | How to fill it in |
|---|---|---|
| `bind_address` | `0.0.0.0` | IP address the server listens on. `0.0.0.0` = all interfaces (needed for your phone to reach it). `127.0.0.1` = this computer only. It must be an IP address, not a host name. Leave it alone in Docker. |
| `port` | `8765` | TCP port of the API and the app. In Docker, change `RWC_PORT` in `.env` instead. |
| `token` | *(generated)* | API token. Leave it empty: a random one is generated on start-up, saved here and in `RemoteWebControl/token.txt` (inside Cura's data folder), and written to the log. To change it, empty it and restart; you will have to pair your phones again. |
| `cors_origins` | *(empty)* | Only if you use the API from a web page hosted at **another** address: its origins, separated by commas (for example `http://192.168.1.50:5173`). The bundled app does not need it. `*` allows any origin (not recommended). |
| `max_upload_mb` | `200` | Maximum size of an upload (all the STLs sent at once), in MB. |
| `job_retention_days` | `7` | Days jobs are kept. Older ones are deleted when Cura starts. `0` = all are deleted on start-up. |

If a value is invalid, the default is used and Cura's log says so (search for `[RemoteWebControl]`).

## Using the app

- **Jobs**: the list of jobs and their state. `+ New` to upload an STL.
- **New job**: choose one or more STLs (small parts can be printed together on the same plate), the printer and the profile. "Auto-orient on upload" needs the Auto Orientation plugin in Cura.
- **Place**: a 3D view of the build plate. Tap a model (in the view or in the list) to select it, rotate it 90° around each axis or tap "Auto-orient". You can also add more STLs, duplicate a model to print several copies, remove one, or "Arrange all". The models always rest on the plate, and Cura's own arrange makes room when a model would overlap another one or fall outside the plate. A model that does not fit turns red and the app explains why.
- **Settings**: Cura's settings, with their visibility levels and warnings. A dot (orange in the dark theme, blue in the light one) marks the settings changed in the job, and ↺ puts them back to the profile value. Changes are saved **in the job only**, never in your Cura profiles.
- **Slice**: progress and cancel; when done, the print time, material, "Download G-code" and "Share" (AirDrop, Files...).

The app is in English or Spanish, following the phone's language. Setting names come from Cura's own translations in the same language.

The button at the top right switches the theme: **dark** (the default), **light** (Cura's own light palette) or **system** (follows the phone's light/dark mode). The choice is remembered on each phone.

## Security

- **Intended use**: a trusted home network. The API, the app and noVNC use **plain HTTP**. **Do not expose them to the internet** (no port forwarding on your router). For remote access, use a VPN (for example Tailscale or WireGuard) or an HTTPS reverse proxy in front.
- **The token gives full access to the API.** Treat it like a password. Don't publish it; `.env` and `cura-data/` are in `.gitignore`.
- **noVNC** gives full control of Cura. Only turn it on if you need it, with your own password (VNC only uses the first 8 characters).
- The API rejects setting values starting with `=`, because Cura would run them as code.

## Known limitations

- **Empty build plate**: Remote Web Control needs the build plate of Cura's GUI to be empty; if there are models on it, the API answers `409 scene_not_empty`. It is meant for a Cura dedicated to this.
- **Cura dialogs**: if Cura has a modal dialog open, requests may return `503` until it is closed (in Docker, through noVNC).
- **Temporary preferences**: during each operation two Cura preferences are changed, and restored afterwards:
  - the Auto Orientation plugin's automatic orientation on load, which would rotate the model in the background;
  - the "Discard or keep changes" question, so that its dialog does not show up.
- **Closing Cura mid-operation**: if Cura closes or hangs in the middle of an operation, the job's printer or profile may be left active. On start-up, jobs that were queued or slicing are marked as failed (`interrupted`).
- **Undo in the GUI**: after a job, Ctrl+Z in the GUI could bring the job's model back.
- **No offline mode**: without HTTPS there is no service worker. The app works on the local network and can be added to the home screen.
- **G-code thumbnail**: it comes from the printer's `CreateThumbnail` post-processing script, if you have it set up. It needs Cura to be able to render: with the window minimised it may be missing, just like in the GUI.
- **Windows and determinism**: the Windows slicing engine is not deterministic (two slices of the same job may order some moves within a layer differently). On Linux/Docker the output is always the same.

## Development

```
RemoteWebControl/    the plugin (Python) and the web app (RemoteWebControl/web)
docker/              Dockerfile, container start-up and resource pruning
docs/API.md          API reference
docs/DESIGN.md       design and technical notes on Cura's internal APIs
scripts/             configuration export, smoke test, icon generation
samples/             sample STL
tests/               tests of the parts that do not depend on Cura
```

- **Tests** (Python 3.12, like Cura's; with [uv](https://docs.astral.sh/uv/) there is nothing to install):
  ```sh
  uv run --no-project --python 3.12 --with pytest --with "numpy<2" -- python -m pytest
  ```
- **Smoke test** against a running Cura, local or in Docker (bash + curl):
  ```sh
  scripts/smoke_test.sh
  RWC_URL=http://192.168.1.10:8765 RWC_TOKEN=... PRINTER_ID="My printer" scripts/smoke_test.sh
  ```
  It runs the whole flow: printers, profiles, uploading two STLs, rotating, duplicating, arranging, auto-orienting, changing a setting, slicing and downloading.
- **Icons**: `uv run --no-project --python 3.12 --with pillow -- python scripts/make_icons.py`.
- **Translations**: the app's texts are in [`RemoteWebControl/web/js/i18n.js`](RemoteWebControl/web/js/i18n.js). To add a language, add a block with the same keys and extend the language detection at the top of the file.
- **No external dependencies**: the plugin only uses the Python standard library bundled with Cura, plus numpy. The app needs no build step: HTML, CSS and JavaScript with native modules.

How it works inside, and the problems found with Cura's APIs, are described in [docs/DESIGN.md](docs/DESIGN.md).

## License and credits

Remote Web Control for Cura is released under the **GNU Lesser General Public License v3.0** ([LICENSE](LICENSE); the LGPL builds on the GPLv3, included in [COPYING](COPYING)). This is the same license as Cura and Uranium, from which the plugin reproduces small pieces of logic to behave like the GUI.

Included in this repository:
- [three.js](https://threejs.org/) 0.170.0 (MIT) and [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) 1.4.4 (MIT), in `RemoteWebControl/web/vendor/` with their licenses.

Downloaded when the Docker image is built (not included here):
- [UltiMaker Cura](https://github.com/Ultimaker/Cura) (LGPLv3), with its CuraEngine slicer (AGPLv3) and other components.
- [noVNC](https://github.com/novnc/noVNC) (MPL-2.0) and [websockify](https://github.com/novnc/websockify) (LGPLv3).
- Ubuntu 24.04 packages.

If you publish a pre-built image, you are redistributing those components and must comply with their licenses (notices and access to the source code). The simplest option is to publish only the `Dockerfile`, as this repository does.

Auto-orientation uses the [Auto Orientation](https://github.com/nallath/CuraOrientationPlugin) plugin (LGPLv3), when installed, which is based on Christoph Schranz's MeshTweaker.

"UltiMaker" and "Cura" are trademarks of UltiMaker. This project is not affiliated with or endorsed by UltiMaker.
