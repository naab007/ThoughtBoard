"""Auto-ingest source code into a codemap (graphify-style).

Python is parsed properly with ast (functions, methods, docstrings, call sites);
JS/TS and C-family files get best-effort regex extraction with brace matching.
Call edges are resolved by simple-name match, preferring same-module targets.
When the candidate count exceeds max_blocks, the most connected (then largest)
blocks win. Layout is layered left-to-right by longest-path call depth.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from . import storage

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".idea", ".vs", "bin", "obj", ".mypy_cache", ".pytest_cache", "site-packages"}
PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs"}
C_EXT = {".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".java"}
MAX_FILE_BYTES = 1_500_000
MAX_FILES = 400
MAX_CODE_LINES = 44
NOT_CALLS = {"if", "for", "while", "switch", "return", "catch", "sizeof", "new",
             "super", "typeof", "assert", "print", "len", "range", "isinstance",
             "str", "int", "float", "list", "dict", "set", "tuple", "type"}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", s.lower()).strip("-")
    return s[:60] or "blk"


def _trim(code: str) -> str:
    lines = code.splitlines()
    if len(lines) > MAX_CODE_LINES:
        rest = len(lines) - MAX_CODE_LINES
        lines = lines[:MAX_CODE_LINES] + [f"# … trimmed ({rest} more lines) …"]
    return "\n".join(lines)


# ------------------------------------------------------------------ python

def _extract_python(path: Path, rel: str) -> list[dict]:
    try:
        src = path.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out, mod = [], path.stem

    def add(node, qual):
        seg = ast.get_source_segment(src, node) or ""
        if not seg.strip():
            return
        doc = ast.get_docstring(node) or ""
        note = " ".join(doc.strip().splitlines()[0].split())[:220] if doc else ""
        calls = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    calls.add(f.id)
                elif isinstance(f, ast.Attribute):
                    calls.add(f.attr)
        out.append({"qual": f"{mod}.{qual}", "simple": qual.split(".")[-1], "module": mod,
                    "file": f"{rel}:{node.lineno}", "code": _trim(seg), "note": note,
                    "lang": "python", "lines": len(seg.splitlines()), "calls": calls - NOT_CALLS})

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(sub, f"{node.name}.{sub.name}")
    return out


# --------------------------------------------------------------- js / c-ish

JS_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\("
    r"|^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?(?:function\b|\()", re.M)
C_RE = re.compile(r"^[A-Za-z_][\w:\*&<>,\.\[\]\s]*?[\s\*&](\w+)\s*\([^;{}]*\)\s*(?:const\s*)?\{", re.M)


def _braced(src: str, start: int) -> str | None:
    i = src.find("{", start)
    if i < 0:
        return None
    depth, j = 0, i
    while j < len(src) and j - i < 40000:
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                line_start = src.rfind("\n", 0, start) + 1
                return src[line_start:j + 1]
        j += 1
    return None


def _extract_regex(path: Path, rel: str, lang: str, rx: re.Pattern) -> list[dict]:
    src = path.read_text(encoding="utf-8-sig", errors="replace")
    out, mod = [], path.stem
    for m in rx.finditer(src):
        name = next((g for g in m.groups() if g), None)
        if not name or name in NOT_CALLS:
            continue
        code = _braced(src, m.start())
        if not code:
            continue
        line = src[:m.start()].count("\n") + 1
        calls = set(re.findall(r"\b(\w+)\s*\(", code)) - {name} - NOT_CALLS
        out.append({"qual": f"{mod}.{name}", "simple": name, "module": mod,
                    "file": f"{rel}:{line}", "code": _trim(code), "note": "",
                    "lang": lang, "lines": len(code.splitlines()), "calls": calls})
    return out


# ------------------------------------------------------------------- ingest

def ingest_code(project: str, codemap_id: str, path: str, include: str | None = None,
                max_blocks: int = 50, replace: bool = False, title: str | None = None,
                description: str = "") -> dict:
    root = Path(path)
    if not root.exists():
        raise storage.StorageError(f"path {path!r} does not exist")
    max_blocks = max(2, min(int(max_blocks), 200))

    if root.is_file():
        files, base = [root], root.parent
    else:
        base = root
        files = [p for p in root.glob(include or "**/*") if p.is_file()]
        files = [f for f in files if not any(part in SKIP_DIRS for part in f.parts)]
    files = [f for f in files if f.suffix.lower() in (PY_EXT | JS_EXT | C_EXT)
             and f.stat().st_size <= MAX_FILE_BYTES]
    files = sorted(set(files))
    truncated_files = len(files) > MAX_FILES
    files = files[:MAX_FILES]
    if not files:
        raise storage.StorageError(
            "no supported source files found — supported: "
            f"{sorted(x.lstrip('.') for x in PY_EXT | JS_EXT | C_EXT)}; "
            "pass include='**/*.py'-style globs to narrow a directory")

    cands: list[dict] = []
    for f in files:
        rel = os.path.relpath(f, base).replace("\\", "/")
        ext = f.suffix.lower()
        if ext in PY_EXT:
            cands += _extract_python(f, rel)
        elif ext in JS_EXT:
            cands += _extract_regex(f, rel, "js", JS_RE)
        else:
            cands += _extract_regex(f, rel, ext.lstrip("."), C_RE)
    if not cands:
        raise storage.StorageError(f"no functions found in {len(files)} scanned file(s)")

    # resolve call edges BEFORE pruning — connectivity drives what survives
    by_simple: dict[str, list] = {}
    for c in cands:
        by_simple.setdefault(c["simple"], []).append(c)
    edges: list[tuple] = []
    for c in cands:
        for name in c["calls"]:
            targets = by_simple.get(name)
            if not targets:
                continue
            same_mod = [t for t in targets if t["module"] == c["module"] and t is not c]
            pick = same_mod[0] if same_mod else (
                targets[0] if len(targets) == 1 and targets[0] is not c else None)
            if pick is not None and pick is not c:
                edges.append((c, pick))

    dropped = 0
    if len(cands) > max_blocks:
        deg: dict[int, int] = {}
        for a, b in edges:
            deg[id(a)] = deg.get(id(a), 0) + 1
            deg[id(b)] = deg.get(id(b), 0) + 1
        ranked = sorted(cands, key=lambda c: (-deg.get(id(c), 0), -min(c["lines"], 80), c["qual"]))
        keep = {id(c) for c in ranked[:max_blocks]}
        dropped = len(cands) - max_blocks
        cands = [c for c in cands if id(c) in keep]
        edges = [(a, b) for a, b in edges if id(a) in keep and id(b) in keep]

    used: set = set()
    idmap: dict[int, str] = {}
    for c in cands:
        base_id = _slug(c["qual"])
        bid, i = base_id, 2
        while bid in used:
            bid = f"{base_id}-{i}"[:64]
            i += 1
        used.add(bid)
        idmap[id(c)] = bid
    edge_ids = list(dict.fromkeys(
        (idmap[id(a)], idmap[id(b)]) for a, b in edges if idmap[id(a)] != idmap[id(b)]))

    # layered layout: longest path from roots, stacked by estimated heights
    layer = {idmap[id(c)]: 0 for c in cands}
    n = len(cands)
    for _ in range(n):
        changed = False
        for a, b in edge_ids:
            if layer[b] < layer[a] + 1 and layer[a] + 1 < n:
                layer[b] = layer[a] + 1
                changed = True
        if not changed:
            break
    groups: dict[int, list] = {}
    for c in cands:
        groups.setdefault(layer[idmap[id(c)]], []).append(c)
    blocks = []
    x = 60
    for L in sorted(groups):
        y = 60
        for c in sorted(groups[L], key=lambda c: c["qual"]):
            est = 165 + min(c["lines"] * 17, 238) + (34 if c["note"] else 0)
            blocks.append({"id": idmap[id(c)], "title": c["qual"], "file": c["file"],
                           "lang": c["lang"], "code": c["code"], "note": c["note"],
                           "pos": {"x": x, "y": y}})
            y += est + 44
        x += 420 + 90

    bmap = {b["id"]: b for b in blocks}
    for a, b2 in edge_ids:
        links = bmap[a].setdefault("links", [])
        if not any(l["to"] == b2 for l in links):
            links.append({"to": b2, "label": "calls"})

    exists = storage._codemap_path(project, codemap_id, must_exist=False).exists()
    if exists and not replace:
        raise storage.StorageError(
            f"codemap {codemap_id!r} already exists in {project!r} — pass replace=True to overwrite")
    desc = description or (f"Auto-ingested from {root} — {len(files)} file(s) scanned"
                           + (f", {dropped} low-connectivity block(s) pruned" if dropped else "")
                           + (", file list truncated" if truncated_files else ""))
    result = storage.save_codemap(project, codemap_id, {
        "schema": storage.CM_SCHEMA, "project": project, "codemap": codemap_id,
        "title": title or f"Code map: {root.name}", "description": desc, "blocks": blocks,
    })
    result.update({"files_scanned": len(files), "blocks_kept": len(blocks),
                   "blocks_pruned": dropped, "call_edges": len(edge_ids)})
    return result
