"""FastAPI application: API + static web UI on one port, background worker in-process."""

from __future__ import annotations

import fcntl
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings, get_settings
from ..db import Database
from ..scheduler import Scheduler
from ..security.auth import AuthService
from ..services.container import Services
from ..services.drafts import recover_interrupted_sends
from ..services.profile import ensure_profile
from ..worker import Worker
from .routes import api, public

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


class InstanceLock:
    """Only one server per data directory, so two processes never run the same queue."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError(
                f"Another Home Agent server is already running with data directory {self.path.parent}. "
                "Stop it first (scripts/service.sh stop)."
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def allowed_hosts(settings: Settings) -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1", "[::1]"}
    hostname = socket.gethostname().lower()
    hosts |= {hostname, f"{hostname}.local"}
    if settings.host not in ("0.0.0.0", "::"):
        hosts.add(settings.host.lower())
    if settings.public_url:
        from urllib.parse import urlsplit

        public_host = urlsplit(settings.public_url).hostname
        if public_host:
            hosts.add(public_host.lower())
    for extra in settings.allowed_hosts.split(","):
        if extra.strip():
            hosts.add(extra.strip().lower())
    return hosts


def create_app(settings: Settings | None = None, *, services: Services | None = None, start_background: bool = True,
               use_lock: bool = True) -> FastAPI:  # fmt: skip
    settings = settings or get_settings()
    hosts = allowed_hosts(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock = InstanceLock(settings.lock_path) if (start_background and use_lock) else None
        if lock:
            lock.acquire()
        db = services.db if services is not None else Database(settings.db_path)
        db.migrate()
        ensure_profile(db)
        svc = services or Services(settings, db)
        app.state.services = svc
        app.state.auth = AuthService(db, settings.session_days)
        if start_background:
            svc.worker = Worker(svc, settings.max_concurrent_tasks)
            svc.scheduler = Scheduler(svc)
            recovered = svc.worker.recover()
            if recovered:
                log.info("re-queued %s interrupted task(s)", recovered)
            try:
                await recover_interrupted_sends(db, svc.gmail)
            except Exception:
                log.exception("could not verify interrupted email sends")
            svc.worker.start()
            svc.scheduler.start()
        log.info("Home Agent %s ready on http://%s:%s", __version__, settings.host, settings.port)
        try:
            yield
        finally:
            if start_background:
                await svc.scheduler.stop()
                await svc.worker.stop()
            await svc.aclose()
            db.close()
            if lock:
                lock.release()

    app = FastAPI(title="Home Agent", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)  # fmt: skip

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        bare = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if bare not in hosts and not bare.endswith(".ts.net"):
            return PlainTextResponse(
                "Host not allowed. Add it to HOME_AGENT_ALLOWED_HOSTS in .env if this is your own address.", status_code=421
            )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        problems = []
        for err in exc.errors()[:5]:
            location = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            problems.append(f"{location}: {err.get('msg')}" if location else str(err.get("msg")))
        return JSONResponse({"detail": "; ".join(problems) or "Invalid request"}, status_code=422)

    app.include_router(public)
    app.include_router(api)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    return app
