"""Remote Web Control for Cura Extension: owns the configuration and the HTTP server lifecycle."""

import json
import os
import socket
from typing import Any, List, Optional

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices

from UM.Extension import Extension
from UM.Logger import Logger
from UM.Message import Message
from UM.Resources import Resources

from . import config as config_module
from .api import build_router
from .config import Config
from .cura_service import CuraService
from .job_service import JobService
from .jobs import JobStore
from .main_thread import MainThreadRunner
from .pairing import Pairing
from .scene_ops import SceneOps
from .scene_worker import SceneWorker
from .server import ApiHttpServer
from .session import read_settings, run_placement, run_slice, settings_diff
from .settings_ops import SettingsOps
from .static_files import StaticFiles

LOG_PREFIX = "[RemoteWebControl] "
DISPLAY_NAME = "Remote Web Control"

# The few texts shown inside Cura, in Cura's language (Spanish or English).
_TEXTS = {
    "en": {
        "pair": "Pair a phone",
        "info": "Show URL and token",
        "start_failed": "Could not start the server on port {0}: {1}",
        "status": "Phone app:\n{0}\n\nAPI:\n{1}",
        "stopped": "The server is NOT running (see cura.log).",
        "token": "{0}\n\nToken:\n{1}\n\nToken file: {2}",
    },
    "es": {
        "pair": "Emparejar móvil",
        "info": "Mostrar URL y token",
        "start_failed": "No se pudo arrancar el servidor en el puerto {0}: {1}",
        "status": "App para el móvil:\n{0}\n\nAPI:\n{1}",
        "stopped": "El servidor NO está en marcha (revisa cura.log).",
        "token": "{0}\n\nToken:\n{1}\n\nFichero del token: {2}",
    },
}


def _log(level: str, message: str) -> None:
    if not message.startswith(LOG_PREFIX):
        message = LOG_PREFIX + message
    Logger.log(level, message)


class RemoteWebControlPlugin(Extension):
    def __init__(self, application: Any) -> None:
        super().__init__()
        self._application = application
        self._preferences = application.getPreferences()
        self._data_dir = os.path.join(Resources.getDataStoragePath(), "RemoteWebControl")
        os.makedirs(self._data_dir, exist_ok = True)

        for name, default in config_module.DEFAULTS.items():
            self._preferences.addPreference(config_module.preference_key(name), default)
        self._config = self._load_config()

        self._service = CuraService(application, self._plugin_version())
        self._runner = MainThreadRunner(application.callLater)
        self._store = JobStore(os.path.join(self._data_dir, "jobs"))
        self._cleanup_jobs()
        self._worker = SceneWorker(_log)
        self._worker.start()
        scene_ops = SceneOps(application, self._service)
        settings_ops = SettingsOps(application)
        runner = self._runner
        self._jobs = JobService(
            self._store, self._worker, runner,
            resolve_profile = self._service.resolve_profile,
            place = lambda request, matrix, auto_orient: run_placement(
                scene_ops, runner, request, matrix, _log, auto_orient = auto_orient),
            slice = lambda request, matrix, output_path, on_progress, is_cancelled: run_slice(
                scene_ops, runner, request, matrix, output_path, _log, on_progress, is_cancelled),
            read_settings = lambda printer_id, profile, overrides, visibility, language, extruder: read_settings(
                scene_ops, settings_ops, runner, printer_id, profile, overrides, visibility, language, extruder, _log),
            settings_diff = lambda printer_id, profile, overrides, scope, extruder, key, value: settings_diff(
                scene_ops, settings_ops, runner, printer_id, profile, overrides, scope, extruder, key, value, _log),
            log = _log,
        )
        self._pairing = Pairing(lambda: self._config.token)
        router = build_router(self._service, self._runner, self._jobs, pairing = self._pairing,
                              app_urls = self._app_urls, get_token = lambda: self._config.token)
        static = StaticFiles(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
        self._server = ApiHttpServer(router, lambda: self._config, _log, static = static)

        language = str(application.getPreferences().getValue("general/language") or "")
        self._texts = _TEXTS["es" if language.startswith("es") else "en"]
        self.setMenuName(DISPLAY_NAME)
        self.addMenuItem(self._texts["pair"], self._open_pairing_page)
        self.addMenuItem(self._texts["info"], self._show_connection_info)

        self._preferences.preferenceChanged.connect(self._on_preference_changed)
        application.applicationShuttingDown.connect(self._shutdown)

        self._start_server()

    # ------------------------------------------------------------------ config

    def _load_config(self) -> Config:
        raw = {name: self._preferences.getValue(config_module.preference_key(name)) for name in config_module.DEFAULTS}
        config, warnings = config_module.parse_config(raw)
        for warning in warnings:
            _log("w", warning)
        if not config.token:
            token = config_module.generate_token()
            self._preferences.setValue(config_module.preference_key("token"), token)
            config = Config(config.bind_address, config.port, token, config.cors_origins,
                            config.max_upload_mb, config.job_retention_days)
            _log("i", "Generated a new API token.")
        self._write_token_file(config.token)
        return config

    def _write_token_file(self, token: str) -> None:
        path = os.path.join(self._data_dir, "token.txt")
        try:
            with open(path, "w", encoding = "utf-8") as f:
                f.write(token + "\n")
        except OSError as e:
            _log("e", "Could not write {0}: {1}".format(path, e))

    def _on_preference_changed(self, key: str) -> None:
        if not key.startswith(config_module.PREFERENCE_PREFIX):
            return
        old = self._config
        self._config = self._load_config()
        if (old.bind_address, old.port) != (self._config.bind_address, self._config.port):
            _log("i", "Address or port changed; restarting the HTTP server.")
            self._stop_server()
            self._start_server()

    @staticmethod
    def _plugin_version() -> str:
        try:
            with open(os.path.join(os.path.dirname(__file__), "plugin.json"), encoding = "utf-8") as f:
                return str(json.load(f).get("version", "unknown"))
        except (OSError, ValueError):
            return "unknown"

    # ------------------------------------------------------------------ server

    def _start_server(self) -> None:
        try:
            self._server.start()
        except OSError as e:
            _log("e", "Could not start the HTTP server on {0}:{1}: {2}".format(self._config.bind_address, self._config.port, e))
            Message(self._texts["start_failed"].format(self._config.port, e),
                    title = DISPLAY_NAME, message_type = Message.MessageType.ERROR).show()
            return
        _log("i", "HTTP API listening on {0}".format(", ".join(self._urls())))
        _log("i", "API token: {0} (also in {1})".format(self._config.token, os.path.join(self._data_dir, "token.txt")))

    def _cleanup_jobs(self) -> None:
        try:
            result = self._store.cleanup(self._config.job_retention_days)
        except OSError as e:
            _log("e", "Job clean-up failed: {0}".format(e))
            return
        if result["removed"] or result["interrupted"]:
            _log("i", "Job clean-up: removed {0} old job(s), marked {1} interrupted job(s) as failed.".format(
                len(result["removed"]), len(result["interrupted"])))

    def _shutdown(self) -> None:
        self._stop_server()
        self._worker.stop(timeout = 2.0)

    def _stop_server(self) -> None:
        if self._server.is_running:
            self._server.stop()
            _log("i", "HTTP API stopped.")

    def _urls(self) -> List[str]:
        return [url + "api" for url in self._app_urls()] + ["http://127.0.0.1:{0}/api".format(self._config.port)] \
            if self._config.bind_address in ("0.0.0.0", "::") else [url + "api" for url in self._app_urls()]

    def _app_urls(self) -> List[str]:
        """URLs of the web app that a phone on the LAN can open. In Docker the plugin only sees its
        container address, so RWC_PUBLIC_URL (e.g. http://192.168.1.10:8765/) can override it."""
        public_url = os.environ.get("RWC_PUBLIC_URL", "").strip()
        if public_url:
            return [public_url.rstrip("/") + "/"]
        host = self._config.bind_address
        hosts = [_lan_ip() or socket.gethostname()] if host in ("0.0.0.0", "::") else [host]
        return ["http://{0}:{1}/".format(h, self._config.port) for h in hosts]

    def _open_pairing_page(self) -> None:
        if not self._server.is_running:
            self._show_connection_info()
            return
        host = "127.0.0.1" if self._config.bind_address in ("0.0.0.0", "::") else self._config.bind_address
        QDesktopServices.openUrl(QUrl("http://{0}:{1}/pair.html".format(host, self._config.port)))

    def _show_connection_info(self) -> None:
        if self._server.is_running:
            status = self._texts["status"].format("\n".join(self._app_urls()), "\n".join(self._urls()))
        else:
            status = self._texts["stopped"]
        text = self._texts["token"].format(
            status, self._config.token, os.path.join(self._data_dir, "token.txt"))
        Message(text, lifetime = 0, title = DISPLAY_NAME).show()


def _lan_ip() -> Optional[str]:
    """IP of the interface used for outgoing traffic. connect() on UDP sends no packets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed.
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
