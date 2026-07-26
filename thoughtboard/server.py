"""ThoughtBoard MCP server.

Runs the MCP tool surface over stdio for Claude Code AND starts the web portal
(127.0.0.1:7793 by default, override THOUGHTBOARD_PORT) in a daemon thread —
the portal is reachable for as long as the MCP process lives. If the port is
already bound (another Claude Code session's instance owns it), this instance
serves tools only and points portal_info at the running portal.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

from mcp.server.fastmcp import FastMCP

from . import storage
from .portal import PORT_DEFAULT, run_portal

PORT = int(os.environ.get("THOUGHTBOARD_PORT", PORT_DEFAULT))
mcp = FastMCP("thoughtboard")

_portal_state = {"role": "unknown"}


def _start_portal() -> None:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", PORT))
        probe.close()
    except OSError:
        _portal_state["role"] = "external"  # another instance already serves it
        print(f"[thoughtboard] portal already running on 127.0.0.1:{PORT}, serving tools only",
              file=sys.stderr)
        return
    _portal_state["role"] = "owner"
    threading.Thread(target=run_portal, args=(PORT,), daemon=True,
                     name="thoughtboard-portal").start()
    print(f"[thoughtboard] portal at http://127.0.0.1:{PORT}/", file=sys.stderr)


def _lan_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


# ------------------------------------------------------------------ tools

@mcp.tool()
def portal_info() -> dict:
    """Portal URL(s), LAN-access state, data directory and whether this MCP
    instance hosts the portal."""
    lan = storage.lan_enabled()
    return {
        "url": f"http://127.0.0.1:{PORT}/",
        "lan_access": lan,
        "lan_urls": [f"http://{ip}:{PORT}/" for ip in _lan_ips()] if lan else [],
        "data_dir": str(storage.data_root()),
        "portal_host": _portal_state["role"],
        "board_url_pattern": f"http://127.0.0.1:{PORT}/board/<project>/<map_id>",
    }


@mcp.tool()
def set_lan_access(enabled: bool) -> dict:
    """Expose the web portal to the local network (bind 0.0.0.0), or return it to
    loopback-only. Takes effect without a restart: the running portal process
    watches the config and rebinds itself within ~4 seconds (a few seconds of
    downtime — open boards retry automatically). LAN mode still enforces:
    clients must have private/loopback IPs, the Host header must be a private IP
    literal on the portal port (blocks DNS rebinding), and writes must be
    same-origin. If LAN devices still can't connect, allow Python through
    Windows Defender Firewall for private networks."""
    cfg = storage.load_config()
    cfg["lan_access"] = bool(enabled)
    storage.save_config(cfg)
    return {
        "lan_access": bool(enabled),
        "applies": "portal rebinds within ~4s (no restart needed)",
        "lan_urls": [f"http://{ip}:{PORT}/" for ip in _lan_ips()] if enabled else [],
        "note": ("Reachable from devices on your LAN. If not, add a Windows Firewall "
                 "inbound rule for python.exe on private networks."
                 if enabled else "Portal is loopback-only again."),
    }


@mcp.tool()
def list_projects() -> list[dict]:
    """List all ThoughtBoard projects with their maps and research docs."""
    return storage.list_projects()


@mcp.tool()
def create_project(slug: str, title: str) -> dict:
    """Create a new project. slug: kebab-case id, title: human-readable name."""
    return storage.create_project(slug, title)


@mcp.tool()
def get_map(project: str, map_id: str) -> dict:
    """Read a full mindmap (thoughtboard/v1: nodes with id/title/description/parent/status/tags/links/pos)."""
    return storage.get_map(project, map_id)


@mcp.tool()
def dump_board(project: str, map_id: str | None = None) -> str:
    """Token-friendly plain-text dump — the cheap way to READ a board.
    With map_id: that map as an indented tree, one line per node:
    `id [status/PRIORITY] title #tags — description | → links` (children sorted by
    priority; pos and p3-default omitted). Without map_id: every map in the project
    plus the research doc list. Prefer this over get_map unless you need to edit."""
    if map_id is not None:
        return storage.dump_map_text(storage.get_map(project, map_id))
    parts = []
    proj = next((p for p in storage.list_projects() if p["slug"] == project), None)
    if proj is None:
        raise storage.StorageError(
            f"project {project!r} not found. Existing: "
            f"{[p['slug'] for p in storage.list_projects()] or 'none'}")
    for m in proj["maps"]:
        parts.append(storage.dump_map_text(storage.get_map(project, m["id"])))
    for t in proj.get("timelines", []):
        parts.append(storage.dump_timeline_text(storage.get_timeline(project, t["id"])))
    if proj["research"]:
        parts.append("research docs (read with get_research): "
                     + ", ".join(f'{r["doc"]} "{r["title"]}"' for r in proj["research"]))
    return "\n\n".join(parts) if parts else f"project {project!r} has no maps yet"


@mcp.tool()
def create_map(project: str, map_id: str, title: str, description: str = "") -> dict:
    """Create a new empty mindmap in a project."""
    return storage.create_map(project, map_id, title, description)


@mcp.tool()
def delete_map(project: str, map_id: str) -> dict:
    """Delete a mindmap permanently."""
    return storage.delete_map(project, map_id)


@mcp.tool()
def upsert_node(project: str, map_id: str, node_id: str, title: str | None = None,
                description: str | None = None, parent: str | None = "__unset__",
                status: str | None = None, tags: list[str] | None = None,
                priority: str | None = None) -> dict:
    """Create or update a node. Creating requires title (and parent, unless the map is
    empty — the first node becomes the root). Only provided fields are changed.
    status: idea|researching|planned|in-progress|done|blocked.
    priority: p0..p6 — p0 is highest/urgent, p3 is the default (stored implicitly),
    p6 is someday/icebox. Passing parent re-parents the node (cycle-guarded).
    Node ids are stable — never rename them."""
    return storage.upsert_node(project, map_id, node_id, title=title,
                               description=description, parent=parent,
                               status=status, tags=tags, priority=priority)


@mcp.tool()
def delete_node(project: str, map_id: str, node_id: str) -> dict:
    """Delete a leaf node (refused while it has children). Inbound links are removed."""
    return storage.delete_node(project, map_id, node_id)


@mcp.tool()
def link_nodes(project: str, map_id: str, from_id: str, to_id: str, label: str = "") -> dict:
    """Add (or relabel) a cross-link between two nodes, e.g. label='depends on'."""
    return storage.link_nodes(project, map_id, from_id, to_id, label)


@mcp.tool()
def unlink_nodes(project: str, map_id: str, from_id: str, to_id: str) -> dict:
    """Remove a cross-link."""
    return storage.unlink_nodes(project, map_id, from_id, to_id)


@mcp.tool()
def create_timeline(project: str, timeline_id: str, title: str, description: str = "") -> dict:
    """Create a new empty timeline board — a horizontal line with text/image entries.
    View at /timeline/<project>/<timeline_id> on the portal."""
    return storage.create_timeline(project, timeline_id, title, description)


@mcp.tool()
def get_timeline(project: str, timeline_id: str) -> dict:
    """Read a full timeline (thoughtboard-timeline/v1: entries with id/pos/side/
    title/text/label/image). For a cheap read use dump_board(project) which
    includes timelines as text."""
    return storage.get_timeline(project, timeline_id)


@mcp.tool()
def delete_timeline(project: str, timeline_id: str) -> dict:
    """Delete a timeline and its stored images permanently."""
    return storage.delete_timeline(project, timeline_id)


@mcp.tool()
def upsert_timeline_entry(project: str, timeline_id: str, entry_id: str,
                          title: str | None = None, text: str | None = None,
                          pos: float | None = None, side: str | None = None,
                          label: str | None = None, image_path: str | None = None) -> dict:
    """Create or update a timeline entry. pos = position along the line (px at zoom 1,
    left→right; omit on create to append after the last entry). side: up|down.
    label = short tick caption (e.g. a date). image_path = local file to attach —
    it is copied into the timeline's image store."""
    return storage.upsert_timeline_entry(project, timeline_id, entry_id, title=title,
                                         text=text, pos=pos, side=side, label=label,
                                         image_path=image_path)


@mcp.tool()
def delete_timeline_entry(project: str, timeline_id: str, entry_id: str) -> dict:
    """Delete a timeline entry (its image is removed too if nothing else uses it)."""
    return storage.delete_timeline_entry(project, timeline_id, entry_id)


@mcp.tool()
def list_research(project: str) -> list[dict]:
    """List research docs in a project."""
    return storage.list_research(project)


@mcp.tool()
def get_research(project: str, doc: str) -> str:
    """Read a research doc (markdown, may have YAML frontmatter)."""
    return storage.get_research(project, doc)


@mcp.tool()
def write_research(project: str, doc: str, content: str) -> dict:
    """Create or overwrite a research doc. Use frontmatter: title, date, tags,
    nodes (related map node ids)."""
    return storage.write_research(project, doc, content)


def main() -> None:
    _start_portal()
    mcp.run()


if __name__ == "__main__":
    main()
