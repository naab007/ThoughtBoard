"""File-backed storage for ThoughtBoard projects, maps and research docs.

Data lives OUTSIDE the install dir (default ~/.thoughtboard, override with
THOUGHTBOARD_DATA) so redeploys never touch user boards.

Layout:
    <root>/boards/<project>/project.json
    <root>/boards/<project>/maps/<map_id>.json      (schema thoughtboard/v1)
    <root>/boards/<project>/research/<doc>.md

All slugs (project, map_id, node ids, doc names) pass through check_slug() —
the single chokepoint that rejects path traversal, drive letters and UNC paths.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path

SCHEMA = "thoughtboard/v1"
STATUSES = ["idea", "researching", "planned", "in-progress", "done", "blocked"]
PRIORITIES = ["p0", "p1", "p2", "p3", "p4", "p5", "p6"]  # p0 highest, p3 default, p6 someday
PRIORITY_DEFAULT = "p3"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class StorageError(ValueError):
    """Raised with a message that teaches the caller what IS valid."""


def data_root() -> Path:
    root = Path(os.environ.get("THOUGHTBOARD_DATA", str(Path.home() / ".thoughtboard")))
    (root / "boards").mkdir(parents=True, exist_ok=True)
    return root


def check_slug(kind: str, value: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise StorageError(
            f"invalid {kind} {value!r}: must be a kebab/snake slug matching "
            "[a-z0-9][a-z0-9_-]* (max 64 chars, lowercase, no spaces, dots or path separators)"
        )
    return value


def _today() -> str:
    return datetime.date.today().isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- projects

def _project_dir(project: str, must_exist: bool = True) -> Path:
    check_slug("project", project)
    d = data_root() / "boards" / project
    if must_exist and not (d / "project.json").exists():
        raise StorageError(
            f"project {project!r} not found. Existing projects: "
            f"{[p['slug'] for p in list_projects()] or 'none'} — create one with create_project"
        )
    return d


def list_projects() -> list[dict]:
    out = []
    for pj in sorted((data_root() / "boards").glob("*/project.json")):
        try:
            meta = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        slug = pj.parent.name
        out.append({
            "slug": slug,
            "title": meta.get("title", slug),
            "maps": [_map_summary(p) for p in sorted(pj.parent.glob("maps/*.json"))],
            "research": [_research_summary(p) for p in sorted(pj.parent.glob("research/*.md"))],
        })
    return out


def create_project(slug: str, title: str) -> dict:
    check_slug("project", slug)
    if not title or not isinstance(title, str):
        raise StorageError("project title is required (a short human-readable name)")
    d = data_root() / "boards" / slug
    if (d / "project.json").exists():
        raise StorageError(f"project {slug!r} already exists — use list_projects to see it")
    _atomic_write(d / "project.json", json.dumps(
        {"slug": slug, "title": title, "created": _today()}, indent=2) + "\n")
    (d / "maps").mkdir(exist_ok=True)
    (d / "research").mkdir(exist_ok=True)
    return {"slug": slug, "title": title}


# ------------------------------------------------------------------- maps

def _map_path(project: str, map_id: str, must_exist: bool = True) -> Path:
    d = _project_dir(project)
    check_slug("map id", map_id)
    p = d / "maps" / f"{map_id}.json"
    if must_exist and not p.exists():
        have = [q.stem for q in sorted((d / "maps").glob("*.json"))]
        raise StorageError(
            f"map {map_id!r} not found in project {project!r}. "
            f"Existing maps: {have or 'none'} — create one with create_map"
        )
    return p


def _map_summary(p: Path) -> dict:
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"id": p.stem, "title": p.stem, "updated": "", "nodes": 0}
    return {"id": p.stem, "title": m.get("title", p.stem),
            "updated": m.get("updated", ""), "nodes": len(m.get("nodes", []))}


def validate_map(m: dict) -> None:
    if not isinstance(m, dict) or not isinstance(m.get("nodes"), list):
        raise StorageError("map must be an object with a 'nodes' array (schema thoughtboard/v1)")
    if not isinstance(m.get("title"), str) or not m["title"]:
        raise StorageError("map 'title' is required")
    ids = [n.get("id") for n in m["nodes"]]
    for i in ids:
        check_slug("node id", i)
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise StorageError(f"duplicate node ids: {sorted(dupes)} — node ids must be unique")
    idset = set(ids)
    roots = [n for n in m["nodes"] if not n.get("parent")]
    if m["nodes"] and len(roots) != 1:
        raise StorageError(
            f"map must have exactly one root node (parent: null); found {len(roots)}"
        )
    for n in m["nodes"]:
        if not isinstance(n.get("title"), str) or not n["title"]:
            raise StorageError(f"node {n.get('id')!r}: 'title' is required")
        if n.get("parent") and n["parent"] not in idset:
            raise StorageError(
                f"node {n['id']!r}: parent {n['parent']!r} does not exist in this map"
            )
        st = n.get("status", "idea")
        if st not in STATUSES:
            raise StorageError(f"node {n['id']!r}: status {st!r} invalid — one of {STATUSES}")
        pr = n.get("priority", PRIORITY_DEFAULT)
        if pr not in PRIORITIES:
            raise StorageError(f"node {n['id']!r}: priority {pr!r} invalid — one of {PRIORITIES} "
                               f"(p0 highest, {PRIORITY_DEFAULT} default, p6 someday)")
        for l in n.get("links", []):
            if l.get("to") not in idset:
                raise StorageError(
                    f"node {n['id']!r}: link target {l.get('to')!r} does not exist in this map"
                )
        pos = n.get("pos")
        if pos is not None and not (isinstance(pos, dict)
                                    and isinstance(pos.get("x"), (int, float))
                                    and isinstance(pos.get("y"), (int, float))):
            raise StorageError(f"node {n['id']!r}: 'pos' must be an object {{x, y}} of numbers")


def get_map(project: str, map_id: str) -> dict:
    p = _map_path(project, map_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StorageError(f"map file {p.name} is corrupt JSON: {e}") from e


def save_map(project: str, map_id: str, m: dict) -> dict:
    _map_path(project, map_id, must_exist=False)  # slug + project checks
    m = dict(m)
    m.setdefault("schema", SCHEMA)
    m["project"], m["map"] = project, map_id
    validate_map(m)
    m["updated"] = _today()
    _atomic_write(_map_path(project, map_id, must_exist=False),
                  json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    return {"project": project, "map": map_id, "updated": m["updated"],
            "nodes": len(m["nodes"])}


def create_map(project: str, map_id: str, title: str, description: str = "") -> dict:
    p = _map_path(project, map_id, must_exist=False)
    if p.exists():
        raise StorageError(f"map {map_id!r} already exists in {project!r} — use get_map to read it")
    return save_map(project, map_id, {
        "schema": SCHEMA, "project": project, "map": map_id,
        "title": title, "description": description, "nodes": [],
    })


def delete_map(project: str, map_id: str) -> dict:
    p = _map_path(project, map_id)
    p.unlink()
    return {"deleted": map_id, "project": project}


# ------------------------------------------------------------------- nodes

def upsert_node(project: str, map_id: str, node_id: str, *, title=None, description=None,
                parent="__unset__", status=None, tags=None, priority=None) -> dict:
    m = get_map(project, map_id)
    check_slug("node id", node_id)
    node = next((n for n in m["nodes"] if n["id"] == node_id), None)
    creating = node is None
    if creating:
        if not title:
            raise StorageError(f"creating node {node_id!r} requires a title")
        node = {"id": node_id, "title": title, "description": description or "",
                "parent": None, "status": status or "idea"}
        if m["nodes"] and parent == "__unset__":
            raise StorageError(
                "new non-first node requires a parent — pass parent=<existing node id> "
                f"(existing: {[n['id'] for n in m['nodes']]})"
            )
        m["nodes"].append(node)
    if title is not None:
        node["title"] = title
    if description is not None:
        node["description"] = description
    if status is not None:
        node["status"] = status
    if tags is not None:
        node["tags"] = list(tags)
    if priority is not None:
        if priority == PRIORITY_DEFAULT:
            node.pop("priority", None)
        else:
            node["priority"] = priority
    if parent != "__unset__":
        # cycle guard: walk up from the new parent
        seen, cur = set(), parent
        by_id = {n["id"]: n for n in m["nodes"]}
        while cur:
            if cur == node_id:
                raise StorageError(f"parent {parent!r} would create a cycle through {node_id!r}")
            if cur in seen:
                break
            seen.add(cur)
            cur = (by_id.get(cur) or {}).get("parent")
        node["parent"] = parent
    save_map(project, map_id, m)
    return {"node": node, "created": creating}


def delete_node(project: str, map_id: str, node_id: str) -> dict:
    m = get_map(project, map_id)
    if not any(n["id"] == node_id for n in m["nodes"]):
        raise StorageError(
            f"node {node_id!r} not found — existing: {[n['id'] for n in m['nodes']]}"
        )
    kids = [n["id"] for n in m["nodes"] if n.get("parent") == node_id]
    if kids:
        raise StorageError(
            f"node {node_id!r} has children {kids} — delete or re-parent them first "
            "(upsert_node with a new parent)"
        )
    removed_links = 0
    for n in m["nodes"]:
        if n.get("links"):
            before = len(n["links"])
            n["links"] = [l for l in n["links"] if l.get("to") != node_id]
            removed_links += before - len(n["links"])
            if not n["links"]:
                n.pop("links")
    m["nodes"] = [n for n in m["nodes"] if n["id"] != node_id]
    save_map(project, map_id, m)
    return {"deleted": node_id, "inbound_links_removed": removed_links}


def link_nodes(project: str, map_id: str, from_id: str, to_id: str, label: str = "") -> dict:
    m = get_map(project, map_id)
    ids = {n["id"] for n in m["nodes"]}
    for i in (from_id, to_id):
        if i not in ids:
            raise StorageError(f"node {i!r} not found — existing: {sorted(ids)}")
    if from_id == to_id:
        raise StorageError("cannot link a node to itself")
    src = next(n for n in m["nodes"] if n["id"] == from_id)
    links = src.setdefault("links", [])
    existing = next((l for l in links if l["to"] == to_id), None)
    if existing:
        existing["label"] = label
    else:
        links.append({"to": to_id, "label": label})
    save_map(project, map_id, m)
    return {"from": from_id, "to": to_id, "label": label, "updated_existing": bool(existing)}


def unlink_nodes(project: str, map_id: str, from_id: str, to_id: str) -> dict:
    m = get_map(project, map_id)
    src = next((n for n in m["nodes"] if n["id"] == from_id), None)
    if not src or not any(l["to"] == to_id for l in src.get("links", [])):
        raise StorageError(f"no link {from_id!r} -> {to_id!r} exists")
    src["links"] = [l for l in src["links"] if l["to"] != to_id]
    if not src["links"]:
        src.pop("links")
    save_map(project, map_id, m)
    return {"unlinked": f"{from_id} -> {to_id}"}


# ---------------------------------------------------------------- research

def _research_path(project: str, doc: str, must_exist: bool = True) -> Path:
    d = _project_dir(project)
    check_slug("research doc name", doc)
    p = d / "research" / f"{doc}.md"
    if must_exist and not p.exists():
        have = [q.stem for q in sorted((d / "research").glob("*.md"))]
        raise StorageError(
            f"research doc {doc!r} not found in {project!r}. Existing docs: {have or 'none'}"
        )
    return p


def _research_summary(p: Path) -> dict:
    title = p.stem
    try:
        text = p.read_text(encoding="utf-8-sig")
        m = re.search(r"^title:\s*(.+)$", text, re.M)
        if m:
            title = m.group(1).strip()
        else:
            h = re.search(r"^#\s+(.+)$", text, re.M)
            if h:
                title = h.group(1).strip()
    except OSError:
        pass
    return {"doc": p.stem, "title": title}


def list_research(project: str) -> list[dict]:
    d = _project_dir(project)
    return [_research_summary(p) for p in sorted((d / "research").glob("*.md"))]


def get_research(project: str, doc: str) -> str:
    return _research_path(project, doc).read_text(encoding="utf-8-sig")


def write_research(project: str, doc: str, content: str) -> dict:
    p = _research_path(project, doc, must_exist=False)
    _atomic_write(p, content if content.endswith("\n") else content + "\n")
    return {"project": project, "doc": doc, "bytes": len(content.encode("utf-8"))}
