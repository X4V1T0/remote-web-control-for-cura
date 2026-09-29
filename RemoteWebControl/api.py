"""HTTP routes of the API. Pure Python: the Cura-dependent parts are injected.

`service` (CuraService) must provide:
    info() -> dict                        (cheap, thread-safe; no Cura access)
    list_printers() -> list               (main thread)
    get_profiles(printer_id) -> dict      (main thread)
`jobs` is a JobService.
"""

from typing import Any, Callable, List, Optional

from .errors import ApiError
from .job_service import JobService
from .main_thread import MainThreadRunner
from .pairing import Pairing
from .server import Request, Response, Router, accepts_gzip, content_disposition, has_bearer_token, is_local_client


def build_router(service: Any, runner: MainThreadRunner, jobs: JobService, pairing: Optional[Pairing] = None,
                 app_urls: Callable[[], List[str]] = lambda: [], get_token: Callable[[], str] = lambda: "") -> Router:
    router = Router()

    def pairing_start(request: Request) -> Response:
        # It hands out the token: only from Cura's own PC, or from someone who already has the token
        # (in Docker every request comes from the container network, never from localhost).
        if not is_local_client(request) and not has_bearer_token(request, get_token()):
            raise ApiError(403, "local_only", "Open the pairing page on the PC running Cura, or send the API token.")
        started = pairing.start()
        return Response.json(dict(started, urls = app_urls(), token = get_token()))

    def pairing_claim(request: Request) -> Response:
        payload = request.json()
        pin = payload.get("pin") if isinstance(payload, dict) else None
        return Response.json({"token": pairing.claim(pin)})

    if pairing is not None:
        router.add("POST", "/api/pairing/start", pairing_start, public = True)
        router.add("POST", "/api/pairing/claim", pairing_claim, public = True)

    def health(request: Request) -> Response:
        # Must not touch the main thread: it has to answer even while Cura is busy.
        return Response.json(dict(service.info(), status = "ok"))

    def printers(request: Request) -> Response:
        return Response.json(runner.run(service.list_printers))

    def profiles(request: Request) -> Response:
        return Response.json(runner.run(service.get_profiles, request.path_params["printer_id"]))

    def create_job(request: Request) -> Response:
        return Response.json(jobs.create(request.headers.get("Content-Type", ""), request.body), status = 201)

    def list_jobs(request: Request) -> Response:
        return Response.json(jobs.list())

    def get_job(request: Request) -> Response:
        return Response.json(jobs.get(request.path_params["job_id"]))

    def delete_job(request: Request) -> Response:
        jobs.delete(request.path_params["job_id"])
        return Response(204, b"", "")

    def job_mesh(request: Request) -> Response:
        return Response(200, jobs.mesh(request.path_params["job_id"]), "application/octet-stream", compressible = True)

    def job_transform(request: Request) -> Response:
        return Response.json(jobs.set_transform(request.path_params["job_id"], request.json()))

    def job_auto_orient(request: Request) -> Response:
        return Response.json(jobs.auto_orient(request.path_params["job_id"]))

    def job_slice(request: Request) -> Response:
        return Response.json(jobs.slice(request.path_params["job_id"]), status = 202)

    def job_cancel(request: Request) -> Response:
        return Response.json(jobs.cancel(request.path_params["job_id"]))

    def job_gcode(request: Request) -> Response:
        path, gz_path, filename = jobs.gcode(request.path_params["job_id"])
        headers = {"Content-Disposition": content_disposition(filename), "Vary": "Accept-Encoding"}
        if gz_path is not None and accepts_gzip(request.headers.get("Accept-Encoding", "")):
            headers["Content-Encoding"] = "gzip"
            path = gz_path
        return Response(200, content_type = "text/x-gcode; charset=utf-8", headers = headers, file_path = path)

    def settings_query(request: Request) -> dict:
        return {name: request.query_value(name) for name in ("visibility", "lang", "extruder")}

    def printer_settings(request: Request) -> Response:
        return Response.json(jobs.printer_settings(request.path_params["printer_id"], settings_query(request)))

    def job_settings(request: Request) -> Response:
        return Response.json(jobs.job_settings(request.path_params["job_id"], settings_query(request)))

    def patch_job_settings(request: Request) -> Response:
        return Response.json(jobs.patch_settings(request.path_params["job_id"], request.json()))

    router.add("GET", "/api/printers/{printer_id}/settings", printer_settings)
    router.add("GET", "/api/jobs/{job_id}/settings", job_settings)
    router.add("PATCH", "/api/jobs/{job_id}/settings", patch_job_settings)
    router.add("GET", "/api/health", health)
    router.add("GET", "/api/printers", printers)
    router.add("GET", "/api/printers/{printer_id}/profiles", profiles)
    router.add("POST", "/api/jobs", create_job)
    router.add("GET", "/api/jobs", list_jobs)
    router.add("GET", "/api/jobs/{job_id}", get_job)
    router.add("DELETE", "/api/jobs/{job_id}", delete_job)
    router.add("GET", "/api/jobs/{job_id}/mesh", job_mesh)
    router.add("PUT", "/api/jobs/{job_id}/transform", job_transform)
    router.add("POST", "/api/jobs/{job_id}/auto-orient", job_auto_orient)
    router.add("POST", "/api/jobs/{job_id}/slice", job_slice)
    router.add("POST", "/api/jobs/{job_id}/cancel", job_cancel)
    router.add("GET", "/api/jobs/{job_id}/gcode", job_gcode)
    return router
