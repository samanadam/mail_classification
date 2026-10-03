"""FastAPI app: JSON API under /api and the built dashboard as static files."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from mailcat import __version__
from mailcat.api.routes import accounts, auth, categories, mails, rules, runs, senders, settings
from mailcat.config import get_config
from mailcat.db import get_engine

log = logging.getLogger(__name__)

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-src 'self' about:; child-src 'self' about:; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
CSRF_HEADER = "x-requested-with"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # A custom header cannot be sent cross-site without a CORS preflight, which we never allow.
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/") \
                and request.headers.get(CSRF_HEADER) != "mailcat":
            return JSONResponse({"detail": "missing X-Requested-With header"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def create_app() -> FastAPI:
    cfg = get_config()
    app = FastAPI(title="Mail categorizer", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(SecurityMiddleware)
    for module in (auth, mails, categories, accounts, rules, settings, runs, senders):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "version": __version__}

    static = cfg.static_dir
    if static and Path(static).is_dir():
        static_path = Path(static)
        app.mount("/assets", StaticFiles(directory=static_path / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> Response:
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            candidate = (static_path / path).resolve()
            if path and candidate.is_file() and static_path.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static_path / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
