"""Localhost dashboard: a static page plus a small, read-only JSON API.

Security model (see also auth.py):
* Listens on 127.0.0.1 only. The host (HOST below) is hard-coded, not configurable.
* Host-header allowlist (127.0.0.1 / localhost) defeats DNS-rebinding attacks
  from web pages in your browser.
* Every /api/* call except the login-code exchange needs the session token
  (Authorization: Bearer). A custom header also blocks cross-site request forgery:
  other origins can't set it without a CORS preflight, and there's no CORS.
* GET only (plus the one POST for login). Nothing can modify or kill anything.
* Strict CSP: scripts, styles and data from this origin only; no inline code,
  no eval, no frames. The page builds the DOM with textContent only.
* No /docs, /redoc or /openapi.json. No access log.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from whyslow import __version__, battery, macos, paths, storage
from whyslow.config import Config
from whyslow.web.auth import CodeVerifier

log = logging.getLogger("whyslow")

HOST = "127.0.0.1"  # never 0.0.0.0; deliberately not configurable
STATIC = Path(__file__).parent / "static"
MAX_POINTS = 1500

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'none'; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}


class LoginRequest(BaseModel):
    code: str = Field(min_length=10, max_length=200)


def create_app(cfg: Config, token: str, db_path: Path | None = None) -> FastAPI:
    db = db_path or paths.db_path()
    verifier = CodeVerifier(token)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/session":
            header = request.headers.get("authorization", "")
            if not (header.startswith("Bearer ") and verifier.check_token(header[7:])):
                response: Response = JSONResponse({"error": "unauthorized"}, status_code=401)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    # Added last = runs first: reject foreign Host headers before anything else.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST, "localhost"])

    def caps() -> macos.Capabilities:
        state = storage.helper_state(db)
        state.setdefault("enabled", cfg.helpers.powermetrics)
        return macos.capabilities(cfg.sampling.system_process_visibility, state)

    def conn():
        try:
            return storage.open_readonly(db)
        except RuntimeError as exc:  # schema mismatch
            raise HTTPException(503, str(exc)) from None

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.post("/api/session")
    def session(body: LoginRequest) -> dict:
        if not verifier.redeem(body.code):
            raise HTTPException(401, "invalid or expired login code; run `whyslow dashboard` again")
        return {"token": token}

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "version": __version__,
            "interval": cfg.sampling.interval_seconds,
            "mode": caps().mode,
            "notes": caps().notes(),
            "cmdline_mode": cfg.privacy.cmdline,
            "detector": {"k": cfg.detector.threshold_k, "sustain": cfg.detector.sustain_samples,
                         "window": cfg.detector.baseline_window_seconds},
            "now": time.time(),
        }

    @app.get("/api/series")
    def series(seconds: float = Query(900, ge=60, le=7 * 86400)) -> dict:
        until = time.time()
        since = until - seconds
        interval = cfg.sampling.interval_seconds
        bucket = 0.0 if seconds / interval <= MAX_POINTS else max(1.0, round(seconds / MAX_POINTS))
        c = conn()
        if c is None:
            data = {name: [] for name in storage.SERIES_COLUMNS}
        else:
            try:
                data = storage.system_series(c, since, until, bucket)
            finally:
                c.close()
        return {"since": since, "until": until, "bucket": bucket or interval, "aggregated": bucket > 0, **data}

    @app.get("/api/spikes")
    def spikes(seconds: float = Query(86400, ge=60, le=365 * 86400),
               limit: int = Query(200, ge=1, le=1000)) -> dict:
        c = conn()
        if c is None:
            return {"spikes": []}
        try:
            return {"spikes": storage.recent_spikes(c, time.time() - seconds, limit)}
        finally:
            c.close()

    @app.get("/api/leaderboard")
    def leaderboard(seconds: float = Query(7 * 86400, ge=3600, le=365 * 86400),
                    limit: int = Query(15, ge=1, le=100)) -> dict:
        c = conn()
        if c is None:
            return {"apps": []}
        try:
            return {"apps": storage.leaderboard(c, time.time() - seconds, limit), "seconds": seconds}
        finally:
            c.close()

    @app.get("/api/battery")
    def battery_estimate(seconds: float = Query(7 * 86400, ge=3600, le=365 * 86400),
                         limit: int = Query(8, ge=1, le=50)) -> dict:
        c = conn()
        if c is None:
            return {"available": False}
        try:
            since = time.time() - seconds
            samples = storage.battery_samples(c, since)
            usage = storage.leaderboard(c, since, 200)
        finally:
            c.close()
        if not samples:
            return {"available": False}
        est = battery.estimate(samples, usage, limit)
        return {
            "available": True,
            "hours_on_battery": est.hours_on_battery,
            "drained_percent": est.drained_percent,
            "baseline_percent": est.baseline_percent,
            "cpu_percent_of_drain": est.cpu_percent_of_drain,
            "sessions": len(est.sessions),
            "basis": est.basis,
            "model": {"usable": est.model.usable, "note": est.model.note,
                      "baseline_per_hour": est.model.baseline_per_hour,
                      "per_cpu_point_per_hour": est.model.per_cpu_point_per_hour,
                      "r_squared": est.model.r_squared, "points": est.model.points},
            "apps": [{"app": a.app, "cpu_seconds": a.cpu_seconds, "wakeups": a.wakeups,
                      "share": a.share, "percent": a.percent} for a in est.apps],
        }

    @app.get("/api/now")
    def now() -> dict:
        c = conn()
        if c is None:
            return {"latest": None, "stale": True}
        try:
            data = storage.latest(c)
        finally:
            c.close()
        stale = data is None or time.time() - data["system"]["ts"] > max(5, 3 * cfg.sampling.interval_seconds)
        return {"latest": data, "stale": stale}

    return app


class DashboardServer:
    """Runs uvicorn in a background thread inside the sampler process."""

    def __init__(self, cfg: Config, token: str, db_path: Path | None = None) -> None:
        self.port = cfg.dashboard.port
        config = uvicorn.Config(
            create_app(cfg, token, db_path), host=HOST, port=self.port, log_config=None, log_level="warning",
            access_log=False, server_header=False, proxy_headers=False, lifespan="off", ws="none",
            http="h11", workers=1,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="dashboard", daemon=True)

    def start(self, timeout: float = 5.0) -> bool:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._thread.is_alive():
            if self._server.started:
                return True
            time.sleep(0.05)
        return False

    def bound_addresses(self) -> list[str]:
        return [sock.getsockname()[0] for srv in self._server.servers for sock in srv.sockets]

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
