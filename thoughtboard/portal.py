"""ThoughtBoard web portal — FastAPI app served by the MCP process.

Two exposure modes, toggled by `lan_access` in ~/.thoughtboard/config.json
(written by the set_lan_access MCP tool — possibly from a different process,
so the supervisor loop in run_portal watches the config and rebinds itself):

- loopback (default): binds 127.0.0.1; only loopback clients and Hosts.
- LAN: binds 0.0.0.0; clients must be loopback or RFC1918-private IPs, the
  Host header must be loopback or a private IP literal on our port (rejecting
  DNS-rebinding hostnames), and state-changing requests must be same-origin.
"""
from __future__ import annotations

import html as html_mod
import ipaddress
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import storage

PORT_DEFAULT = 7793
STATIC = Path(__file__).parent / "static"

_cfg_cache = {"mtime": -1, "lan": False}

# RFC1918 private + RFC6598 shared/CGNAT (Tailscale's 100.64/10 lives there —
# unroutable from the internet, so treat it like the LAN).
_SHARED = ipaddress.ip_network("100.64.0.0/10")


def _local_ip(a: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return a.is_private or (a.version == 4 and a in _SHARED)


def lan_on() -> bool:
    """Current lan_access flag, cached by config-file mtime (cheap per-request)."""
    p = storage.data_root() / "config.json"
    try:
        mt = p.stat().st_mtime_ns
    except OSError:
        mt = None
    if mt != _cfg_cache["mtime"]:
        _cfg_cache["mtime"] = mt
        _cfg_cache["lan"] = storage.lan_enabled()
    return _cfg_cache["lan"]


def build_app(port: int = PORT_DEFAULT) -> FastAPI:
    app = FastAPI(title="ThoughtBoard", docs_url=None, redoc_url=None, openapi_url=None)
    loopback_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}

    def host_allowed(host: str, lan: bool) -> bool:
        if host in loopback_hosts:
            return True
        if not lan:
            return False
        # LAN mode: only private IP literals on our port — hostnames are rejected
        # so a DNS-rebinding domain can never pass.
        name, _, hport = host.partition(":")
        if hport != str(port):
            return False
        try:
            return _local_ip(ipaddress.ip_address(name))
        except ValueError:
            return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        lan = lan_on()
        client = request.client.host if request.client else ""
        try:
            cip = ipaddress.ip_address(client)
        except ValueError:
            cip = None
        if cip is None or not (cip.is_loopback or (lan and _local_ip(cip))):
            return JSONResponse({"error": "client address not allowed"}, status_code=403)
        host = request.headers.get("host", "").lower()
        if not host_allowed(host, lan):
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc.lower() != host:
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

    @app.get("/timeline/{project}/{timeline_id}", response_class=HTMLResponse)
    async def timeline_page(project: str, timeline_id: str):
        storage.check_slug("project", project)
        storage.check_slug("timeline id", timeline_id)
        return page("timeline.html")

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

    @app.get("/api/projects/{project}/maps/{map_id}/version")
    async def api_map_version(project: str, map_id: str):
        return {"version": storage.map_version(project, map_id)}

    @app.put("/api/projects/{project}/maps/{map_id}")
    async def api_save_map(project: str, map_id: str, payload: dict):
        return storage.save_map(project, map_id, payload)

    # ---------------------------------------------------------- timelines
    @app.post("/api/projects/{project}/timelines")
    async def api_create_timeline(project: str, payload: dict):
        return storage.create_timeline(project, payload.get("id", ""),
                                       payload.get("title", ""),
                                       payload.get("description", ""))

    @app.get("/api/projects/{project}/timelines/{timeline_id}")
    async def api_get_timeline(project: str, timeline_id: str):
        return storage.get_timeline(project, timeline_id)

    @app.put("/api/projects/{project}/timelines/{timeline_id}")
    async def api_save_timeline(project: str, timeline_id: str, payload: dict):
        return storage.save_timeline(project, timeline_id, payload)

    @app.get("/api/projects/{project}/timelines/{timeline_id}/version")
    async def api_timeline_version(project: str, timeline_id: str):
        return {"version": storage.timeline_version(project, timeline_id)}

    @app.post("/api/projects/{project}/timelines/{timeline_id}/images")
    async def api_upload_timeline_image(project: str, timeline_id: str, request: Request):
        ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = {v: k for k, v in storage.IMG_EXTS.items()}.get(ct)
        if not ext:
            raise storage.StorageError(
                f"content-type {ct!r} not supported — one of {sorted(storage.IMG_EXTS.values())}")
        data = await request.body()
        return {"image": storage.save_timeline_image(project, timeline_id, data, ext)}

    @app.get("/tlimg/{project}/{timeline_id}/{name}")
    async def api_timeline_image(project: str, timeline_id: str, name: str):
        from fastapi.responses import FileResponse
        p = storage.timeline_image_path(project, timeline_id, name)
        return FileResponse(str(p), media_type=storage.IMG_EXTS[p.suffix.lstrip(".")])

    return app


def run_portal(port: int = PORT_DEFAULT) -> None:
    """Run the portal (blocks). Supervises its own bind address: when the
    lan_access config flag flips — usually written by the set_lan_access MCP
    tool in another process — the server shuts down and rebinds on the new
    address. stdout is never used; MCP stdio owns it."""
    import logging

    import uvicorn

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    while True:
        lan = storage.lan_enabled()
        bind = "0.0.0.0" if lan else "127.0.0.1"
        config = uvicorn.Config(build_app(port), host=bind, port=port,
                                log_config=None, access_log=False)
        server = uvicorn.Server(config)
        rebind = threading.Event()

        def watch():
            while not server.should_exit:
                time.sleep(2)
                if storage.lan_enabled() != lan:
                    rebind.set()
                    server.should_exit = True
                    break

        threading.Thread(target=watch, daemon=True, name="thoughtboard-cfgwatch").start()
        print(f"[thoughtboard] portal on {bind}:{port} (lan {'ON' if lan else 'off'})",
              file=sys.stderr)
        server.run()
        if not rebind.is_set():
            break
