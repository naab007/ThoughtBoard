"""ThoughtBoard web portal — FastAPI app served by the MCP process.

Loopback-only, with Host + Origin allowlist middleware (state-changing requests
must come from the portal's own origin). Pages are self-contained static HTML in
static/; data flows through the JSON API backed by storage.py.
"""
from __future__ import annotations

import html as html_mod
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import storage

PORT_DEFAULT = 7793
STATIC = Path(__file__).parent / "static"


def build_app(port: int = PORT_DEFAULT) -> FastAPI:
    app = FastAPI(title="ThoughtBoard", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @app.middleware("http")
    async def loopback_guard(request: Request, call_next):
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in allowed_origins:
                return JSONResponse({"error": "origin not allowed"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(storage.StorageError)
    async def storage_error(_req, exc: storage.StorageError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    def page(name: str) -> HTMLResponse:
        return HTMLResponse((STATIC / name).read_text(encoding="utf-8"))

    # ------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def home():
        return page("home.html")

    @app.get("/board/{project}/{map_id}", response_class=HTMLResponse)
    async def board(project: str, map_id: str):
        storage.check_slug("project", project)
        storage.check_slug("map id", map_id)
        return page("board.html")

    @app.get("/research/{project}/{doc}", response_class=HTMLResponse)
    async def research_page(project: str, doc: str):
        text = storage.get_research(project, doc)
        try:
            import markdown
            body = markdown.markdown(text, extensions=["fenced_code", "tables"])
        except ImportError:
            body = f"<pre>{html_mod.escape(text)}</pre>"
        meta = storage._research_summary(storage._research_path(project, doc))
        tpl = (STATIC / "research.html").read_text(encoding="utf-8")
        return HTMLResponse(tpl
                            .replace("__TITLE__", html_mod.escape(meta["title"]))
                            .replace("__PROJECT__", html_mod.escape(project))
                            .replace("__BODY__", body))

    # --------------------------------------------------------------- api
    @app.get("/api/projects")
    async def api_projects():
        return storage.list_projects()

    @app.post("/api/projects")
    async def api_create_project(payload: dict):
        return storage.create_project(payload.get("slug", ""), payload.get("title", ""))

    @app.post("/api/projects/{project}/maps")
    async def api_create_map(project: str, payload: dict):
        return storage.create_map(project, payload.get("id", ""),
                                  payload.get("title", ""), payload.get("description", ""))

    @app.get("/api/projects/{project}/maps/{map_id}")
    async def api_get_map(project: str, map_id: str):
        return storage.get_map(project, map_id)

    @app.put("/api/projects/{project}/maps/{map_id}")
    async def api_save_map(project: str, map_id: str, payload: dict):
        return storage.save_map(project, map_id, payload)

    return app


def run_portal(port: int = PORT_DEFAULT) -> None:
    """Run the portal in the current thread (blocks). stdout is never used —
    MCP stdio owns it — so all uvicorn logging goes to stderr."""
    import logging
    import sys

    import uvicorn

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    config = uvicorn.Config(build_app(port), host="127.0.0.1", port=port,
                            log_config=None, access_log=False)
    uvicorn.Server(config).run()
