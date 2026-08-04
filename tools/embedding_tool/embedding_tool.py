"""Embedding & Semantic Search Tool (TASK-002).

LangChain `BaseTool` that builds a FAISS-backed semantic index over one or
more repositories and exposes semantic search, technology/route inventory,
and blast-radius dependency analysis to the Deep Agent.

Notes on scope:
- Embeddings default to an offline, deterministic hashing-based backend so
  the tool works without network access or a downloaded model. A real
  provider (Bedrock, HuggingFace, ...) can be injected via the
  `embeddings_model` field for production-quality recall.
- Blast-radius dependency resolution is implemented via Python AST import
  analysis. Other languages are indexed, route-scanned, and risk-tagged,
  but are not currently wired into the dependency graph.
- Persisted context is namespaced per repository: each repo gets its own
  subfolder under `context_dir`, named after the repo (see
  `_resolve_repo_alias`), so indexing/diffing one repo never overwrites
  another repo's saved context. `search` and `get_blast_radius` merge
  across all persisted repos when no specific `repo_paths` is matched,
  giving cross-repo querying for free.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Type

from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.callbacks.manager import CallbackManagerForToolRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LANGUAGE_BY_EXTENSION: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
}

SPLITTER_LANGUAGE: Dict[str, Language] = {
    "python": Language.PYTHON,
    "javascript": Language.JS,
    "typescript": Language.TS,
    "java": Language.JAVA,
    "go": Language.GO,
}

IGNORED_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".deepagent_context", "migrations", ".mypy_cache", ".pytest_cache", "static",
}

RISK_KEYWORD_PATTERNS: Dict[str, List[str]] = {
    "authentication": [r"login", r"authenticate", r"password", r"jwt", r"\bsession\b", r"\btoken\b", r"oauth"],
    "authorization": [r"permission", r"is_admin", r"\brole\b", r"access_control", r"authorize", r"login_required"],
    "cryptographic": [r"hashlib", r"encrypt", r"decrypt", r"\bcrypto\b", r"\bcipher\b", r"\bmd5\b", r"\bsha1\b"],
    "injection": [r"cursor\.execute", r"\.execute\(", r"os\.system", r"subprocess\.", r"eval\(", r"exec\(",
                  r"pickle\.loads", r"yaml\.load\("],
}

_METHODS_KW_RE = re.compile(r'methods\s*=\s*\[([^\]]*)\]', re.IGNORECASE)

# Optional Python string-literal prefix (r'...', rb"...", f'...', etc.) before the opening quote.
_STR_PREFIX = r'(?:[rRbBuUfF]{1,2})?'

ROUTE_PATTERNS = {
    "python-decorator": re.compile(
        r'@\w+\.(route|get|post|put|delete|patch|options|head)\('
        rf'\s*{_STR_PREFIX}[\'"](?P<path>[^\'"]+)[\'"](?P<rest>[^)]*)\)',
        re.IGNORECASE,
    ),
    "django-url": re.compile(rf'\b(?:path|re_path|url)\(\s*{_STR_PREFIX}[\'"](?P<path>[^\'"]*)[\'"]'),
    "express": re.compile(r'\b\w+\.(get|post|put|delete|patch|use)\(\s*[\'"](?P<path>/[^\'"]*)[\'"]', re.IGNORECASE),
    "spring": re.compile(r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:value\s*=\s*)?[\'"](?P<path>[^\'"]+)[\'"]'),
}

# Which route patterns are meaningful for which detected file language.
ROUTE_PATTERNS_BY_LANGUAGE: Dict[str, List[str]] = {
    "python": ["python-decorator", "django-url"],
    "javascript": ["express"],
    "typescript": ["express"],
    "java": ["spring"],
}

FRAMEWORK_MARKERS: Dict[str, List[str]] = {
    "flask": ["from flask", "import flask", "Flask(__name__)"],
    "fastapi": ["from fastapi", "import fastapi", "FastAPI("],
    "django": ["from django", "import django", "DJANGO_SETTINGS_MODULE"],
    "express": ["require('express')", 'require("express")', "from 'express'", 'from "express"'],
    "spring": ["@RestController", "@SpringBootApplication", "org.springframework"],
}


# ---------------------------------------------------------------------------
# Offline deterministic embeddings backend
# ---------------------------------------------------------------------------

class DeterministicHashingEmbeddings(Embeddings):
    """Dependency-free embeddings using a feature-hashing bag-of-words vector.

    Deterministic and offline so the tool is usable/testable without network
    access. Swap in a real provider via `EmbeddingSemanticSearchTool.embeddings_model`.
    """

    _TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")

    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions

    def _tokenize(self, text: str) -> List[str]:
        tokens = self._TOKEN_RE.findall(text)
        expanded: List[str] = []
        for tok in tokens:
            expanded.append(tok.lower())
            for part in tok.split("_"):
                if part and part.lower() != tok.lower():
                    expanded.append(part.lower())
        return expanded

    def _embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dimensions
        tokens = self._tokenize(text)
        for tok in tokens:
            digest = hashlib.sha256(tok.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed(text)


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------

class EmbeddingToolInput(BaseModel):
    action: str = Field(
        description="The tool action to perform: 'index' (build/refresh index), 'search' (semantic search), "
                    "'get_inventory' (tech/route inventory), or 'get_blast_radius' (dependency impact analysis)."
    )
    mode: Optional[str] = Field(
        default="baseline",
        description="Execution mode when action is 'index': 'baseline' (full repo scan) or 'diff' (refresh changed files)."
    )
    query: Optional[str] = Field(
        default=None,
        description="Semantic search query or risk attribute string (e.g., 'JWT verification', 'SQL execution', 'authentication endpoints')."
    )
    repo_paths: Optional[List[str]] = Field(
        default_factory=lambda: ["repo"],
        description="List of repository directory paths for multi-repo or single-repo indexing/queries."
    )
    changed_files: Optional[List[str]] = Field(
        default=None,
        description="List of added/modified/deleted file paths when mode is 'diff'."
    )
    top_k: Optional[int] = Field(
        default=5,
        description="Number of top relevant semantic code snippets to return during search."
    )


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

class EmbeddingSemanticSearchTool(BaseTool):
    name: str = "embedding_semantic_search"
    description: str = (
        "Indexes repositories and performs semantic search over codebase vectors, technology attributes, "
        "route inventories, and blast radius dependency graphs for security reviews."
    )
    args_schema: Type[EmbeddingToolInput] = EmbeddingToolInput

    context_dir: str = "output"
    embed_dimensions: int = 384
    chunk_size: int = 800
    chunk_overlap: int = 100
    embeddings_model: Any = None

    # Per-repo in-memory caches, keyed by repo alias (see `_resolve_repo_alias`).
    _vector_stores: Dict[str, FAISS] = PrivateAttr(default_factory=dict)
    _metadata_stores: Dict[str, Dict[str, Any]] = PrivateAttr(default_factory=dict)
    _dependency_graphs: Dict[str, Dict[str, Any]] = PrivateAttr(default_factory=dict)
    _route_inventories: Dict[str, List[Dict[str, Any]]] = PrivateAttr(default_factory=dict)
    _tech_inventories: Dict[str, Dict[str, Any]] = PrivateAttr(default_factory=dict)
    _file_hashes: Dict[str, Dict[str, str]] = PrivateAttr(default_factory=dict)
    _alias_manifest: Dict[str, str] = PrivateAttr(default_factory=dict)

    # -- LangChain entry point ----------------------------------------------

    def _run(
        self,
        action: str,
        mode: Optional[str] = "baseline",
        query: Optional[str] = None,
        repo_paths: Optional[List[str]] = None,
        changed_files: Optional[List[str]] = None,
        top_k: Optional[int] = 5,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        action = (action or "").strip().lower()
        repo_paths = repo_paths or ["repo"]
        mode = (mode or "baseline").strip().lower()
        top_k = top_k or 5

        print(
            f"[EMBEDDING TOOL INVOKED] action={action!r} mode={mode!r} repo_paths={repo_paths} "
            f"query={query!r} changed_files={changed_files} top_k={top_k}"
        )

        if action == "index":
            if mode == "baseline":
                return self._build_baseline_index(repo_paths)
            elif mode == "diff":
                return self._refresh_diff_index(repo_paths, changed_files or [])
            return f"[ERROR] Invalid mode '{mode}'. Must be 'baseline' or 'diff'."

        elif action == "search":
            if not query:
                return "[ERROR] Query parameter is required for 'search' action."
            return self._perform_semantic_search(query, top_k, repo_paths)

        elif action == "get_inventory":
            return self._generate_inventory(repo_paths)

        elif action == "get_blast_radius":
            if not changed_files:
                return "[ERROR] changed_files parameter is required for 'get_blast_radius' action."
            return self._calculate_blast_radius(changed_files, repo_paths)

        else:
            return f"[ERROR] Unknown action '{action}'. Supported actions: 'index', 'search', 'get_inventory', 'get_blast_radius'."

    # -- Embeddings backend ---------------------------------------------------

    def _get_embeddings(self) -> Embeddings:
        if self.embeddings_model is not None:
            return self.embeddings_model
        return DeterministicHashingEmbeddings(dimensions=self.embed_dimensions)

    # -- Per-repo context namespacing ------------------------------------------
    #
    # Each repo is persisted under its own subfolder, named after the repo
    # (its directory name) whenever possible:
    #
    #   <context_dir>/manifest.json          alias -> absolute source path
    #   <context_dir>/<alias>/faiss_index/
    #   <context_dir>/<alias>/metadata_store.json
    #   <context_dir>/<alias>/blast_radius_graph.json
    #   <context_dir>/<alias>/route_inventory.json
    #   <context_dir>/<alias>/tech_inventory.json
    #   <context_dir>/<alias>/file_hashes.json

    def _context_path(self) -> Path:
        return Path(self.context_dir)

    def _manifest_path(self) -> Path:
        return self._context_path() / "manifest.json"

    def _repo_context_path(self, repo_alias: str) -> Path:
        return self._context_path() / repo_alias / "context" / "embeddings"

    @staticmethod
    def _persist_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=list), encoding="utf-8")

    def _load_manifest(self) -> Dict[str, str]:
        if not self._alias_manifest and self._manifest_path().exists():
            try:
                self._alias_manifest = json.loads(self._manifest_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._alias_manifest = {}
        return self._alias_manifest

    def _save_manifest(self) -> None:
        self._persist_json(self._manifest_path(), self._alias_manifest)

    def _resolve_repo_alias(self, repo_root: Path) -> str:
        """Assign each distinct repo path a stable, human-readable alias (its folder name)."""
        resolved = str(repo_root.resolve())
        manifest = self._load_manifest()
        for alias, path in manifest.items():
            if path == resolved:
                return alias

        base_alias = repo_root.name or "repo"
        alias = base_alias
        if alias in manifest and manifest[alias] != resolved:
            alias = f"{base_alias}_{hashlib.sha256(resolved.encode('utf-8')).hexdigest()[:8]}"

        manifest[alias] = resolved
        self._alias_manifest = manifest
        self._save_manifest()
        return alias

    def _discover_persisted_aliases(self) -> List[str]:
        ctx = self._context_path()
        if not ctx.exists():
            return []
        return sorted(
            p.name for p in ctx.iterdir()
            if p.is_dir() and (self._repo_context_path(p.name) / "metadata_store.json").exists()
        )

    def _requested_aliases(self, repo_paths: List[str]) -> List[str]:
        """Resolve caller-supplied repo_paths (dir paths or bare alias names) to known aliases."""
        aliases: List[str] = []
        manifest = self._load_manifest()
        for entry in repo_paths:
            candidate: Optional[str] = None
            p = Path(entry)
            if p.exists():
                resolved = str(p.resolve())
                for alias, path in manifest.items():
                    if path == resolved:
                        candidate = alias
                        break
            if candidate is None:
                for name in {p.name, entry}:
                    if name and (self._repo_context_path(name) / "metadata_store.json").exists():
                        candidate = name
                        break
            if candidate and candidate not in aliases:
                aliases.append(candidate)
        return aliases

    def _ensure_repo_loaded(self, alias: str, require_index: bool = False) -> Optional[str]:
        repo_dir = self._repo_context_path(alias)
        if alias not in self._vector_stores and (repo_dir / "faiss_index").exists():
            try:
                self._vector_stores[alias] = FAISS.load_local(
                    str(repo_dir / "faiss_index"), self._get_embeddings(), allow_dangerous_deserialization=True
                )
            except Exception as exc:
                if require_index:
                    return f"[ERROR] Failed to load persisted FAISS index for '{alias}': {exc}"
        if alias not in self._metadata_stores and (repo_dir / "metadata_store.json").exists():
            self._metadata_stores[alias] = json.loads((repo_dir / "metadata_store.json").read_text(encoding="utf-8"))
        if alias not in self._dependency_graphs and (repo_dir / "blast_radius_graph.json").exists():
            self._dependency_graphs[alias] = json.loads((repo_dir / "blast_radius_graph.json").read_text(encoding="utf-8"))
        if alias not in self._route_inventories and (repo_dir / "route_inventory.json").exists():
            self._route_inventories[alias] = json.loads((repo_dir / "route_inventory.json").read_text(encoding="utf-8"))
        if alias not in self._tech_inventories and (repo_dir / "tech_inventory.json").exists():
            self._tech_inventories[alias] = json.loads((repo_dir / "tech_inventory.json").read_text(encoding="utf-8"))
        if alias not in self._file_hashes and (repo_dir / "file_hashes.json").exists():
            self._file_hashes[alias] = json.loads((repo_dir / "file_hashes.json").read_text(encoding="utf-8"))

        if require_index and alias not in self._vector_stores:
            return f"[ERROR] No persisted index found for repo '{alias}'. Run action='index' with mode='baseline' first."
        return None

    # -- Repo scanning ---------------------------------------------------------

    @staticmethod
    def _iter_source_files(repo_root: Path):
        for path in repo_root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in LANGUAGE_BY_EXTENSION:
                continue
            if any(part in IGNORED_DIR_NAMES for part in path.relative_to(repo_root).parts[:-1]):
                continue
            yield path

    @staticmethod
    def _node_id(repo_alias: str, repo_root: Path, file_path: Path) -> str:
        return f"{repo_alias}::{file_path.relative_to(repo_root).as_posix()}"

    @staticmethod
    def _module_name_for(repo_root: Path, file_path: Path) -> str:
        rel = file_path.relative_to(repo_root).with_suffix("")
        parts = list(rel.parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    @staticmethod
    def _extract_python_import_candidates(file_path: Path, repo_root: Path) -> Set[str]:
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            return set()

        own_parts = list(file_path.relative_to(repo_root).with_suffix("").parts)
        if own_parts and own_parts[-1] == "__init__":
            own_parts = own_parts[:-1]
        package_parts = own_parts[:-1]

        candidates: Set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    candidates.add(alias.name)

            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    trim = node.level - 1
                    base_parts = package_parts[: len(package_parts) - trim] if trim < len(package_parts) else []
                else:
                    base_parts = []

                if node.module:
                    base_parts = base_parts + node.module.split(".")

                if base_parts:
                    candidates.add(".".join(base_parts))
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    candidates.add(".".join(base_parts + [alias.name]))

        return candidates

    def _detect_frameworks(self, text: str, file_path: Path) -> Set[str]:
        found: Set[str] = set()
        for framework, markers in FRAMEWORK_MARKERS.items():
            if any(marker in text for marker in markers):
                found.add(framework)
        if file_path.name == "package.json":
            try:
                data = json.loads(text)
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                if "express" in deps:
                    found.add("express")
                if "react" in deps:
                    found.add("react")
            except json.JSONDecodeError:
                pass
        return found

    @staticmethod
    def _detect_risk_tags(text: str, rel_path: str) -> Set[str]:
        haystack = f"{rel_path}\n{text}".lower()
        tags: Set[str] = set()
        for category, patterns in RISK_KEYWORD_PATTERNS.items():
            if any(re.search(p, haystack) for p in patterns):
                tags.add(category)
        return tags

    @staticmethod
    def _extract_routes(text: str, rel_path: str, repo_alias: str, language: str) -> List[Dict[str, Any]]:
        routes: List[Dict[str, Any]] = []
        active_patterns = set(ROUTE_PATTERNS_BY_LANGUAGE.get(language, ()))
        if not active_patterns:
            return routes

        def line_of(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        if "python-decorator" in active_patterns:
            for match in ROUTE_PATTERNS["python-decorator"].finditer(text):
                verb = match.group(1).lower()
                path = match.group("path")
                rest = match.group("rest") or ""
                if verb != "route":
                    methods = [verb.upper()]
                else:
                    kw_match = _METHODS_KW_RE.search(rest)
                    if kw_match:
                        methods = [m.strip().strip("'\"").upper() for m in kw_match.group(1).split(",") if m.strip()]
                    else:
                        methods = ["GET"]
                for method in methods:
                    routes.append({
                        "repo": repo_alias, "path": path, "method": method,
                        "file": rel_path, "line": line_of(match.start()), "source": "python-decorator",
                    })

        if "django-url" in active_patterns:
            for match in ROUTE_PATTERNS["django-url"].finditer(text):
                routes.append({
                    "repo": repo_alias, "path": match.group("path"), "method": "ANY",
                    "file": rel_path, "line": line_of(match.start()), "source": "django-url",
                })

        if "express" in active_patterns:
            for match in ROUTE_PATTERNS["express"].finditer(text):
                verb = match.group(1).lower()
                if verb == "use":
                    continue
                routes.append({
                    "repo": repo_alias, "path": match.group("path"), "method": verb.upper(),
                    "file": rel_path, "line": line_of(match.start()), "source": "express",
                })

        if "spring" in active_patterns:
            for match in ROUTE_PATTERNS["spring"].finditer(text):
                g0 = match.group(0)
                method_match = re.search(r'@(Get|Post|Put|Delete|Patch)Mapping', g0)
                method = method_match.group(1).upper() if method_match else "ANY"
                routes.append({
                    "repo": repo_alias, "path": match.group("path"), "method": method,
                    "file": rel_path, "line": line_of(match.start()), "source": "spring",
                })

        return routes

    def _split_text(self, text: str, language: str) -> List[str]:
        if not text.strip():
            return []
        splitter_lang = SPLITTER_LANGUAGE.get(language)
        if splitter_lang is not None:
            splitter = RecursiveCharacterTextSplitter.from_language(
                language=splitter_lang, chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
            )
        else:
            splitter = RecursiveCharacterTextSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        chunks = splitter.split_text(text)
        return [c for c in chunks if c.strip()]

    def _scan_single_repo(self, repo_root: Path, repo_alias: str) -> Dict[str, Any]:
        """Scan one repo root and return its documents/metadata/routes/tech/dependency graph."""
        documents: List[Document] = []
        metadata_store: Dict[str, Any] = {}
        file_hashes: Dict[str, str] = {}
        route_inventory: List[Dict[str, Any]] = []
        dependency_nodes: Dict[str, Dict[str, Any]] = {}

        files = list(self._iter_source_files(repo_root))

        module_map: Dict[str, str] = {}
        for file_path in files:
            if file_path.suffix == ".py":
                module_map[self._module_name_for(repo_root, file_path)] = self._node_id(repo_alias, repo_root, file_path)

        languages: Dict[str, int] = defaultdict(int)
        frameworks: Set[str] = set()
        indexed_files = 0

        for file_path in files:
            node_id = self._node_id(repo_alias, repo_root, file_path)
            rel_path = file_path.relative_to(repo_root).as_posix()
            language = LANGUAGE_BY_EXTENSION[file_path.suffix]
            languages[language] += 1

            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            file_hashes[node_id] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            frameworks.update(self._detect_frameworks(text, file_path))
            route_inventory.extend(self._extract_routes(text, rel_path, repo_alias, language))
            risk_tags = self._detect_risk_tags(text, rel_path)

            imports: Set[str] = set()
            if file_path.suffix == ".py":
                for candidate in self._extract_python_import_candidates(file_path, repo_root):
                    target = module_map.get(candidate)
                    if target and target != node_id:
                        imports.add(target)

            dependency_nodes[node_id] = {
                "repo": repo_alias, "path": rel_path, "language": language,
                "imports": sorted(imports), "imported_by": [], "risk_tags": sorted(risk_tags),
            }

            for i, chunk in enumerate(self._split_text(text, language)):
                chunk_id = f"{node_id}#{i}"
                meta = {
                    "chunk_id": chunk_id, "node_id": node_id, "repo": repo_alias,
                    "path": rel_path, "language": language, "chunk_index": i,
                    "risk_tags": sorted(risk_tags),
                }
                metadata_store[chunk_id] = meta
                documents.append(Document(page_content=chunk, metadata=meta))

            indexed_files += 1

        pkg_json = repo_root / "package.json"
        if pkg_json.exists():
            try:
                frameworks.update(self._detect_frameworks(
                    pkg_json.read_text(encoding="utf-8", errors="ignore"), pkg_json
                ))
            except OSError:
                pass

        for node_id, node in dependency_nodes.items():
            for dep in node["imports"]:
                if dep in dependency_nodes:
                    dependency_nodes[dep]["imported_by"].append(node_id)
        for node in dependency_nodes.values():
            node["imported_by"] = sorted(set(node["imported_by"]))

        tech_info = {"path": str(repo_root), "languages": dict(languages), "frameworks": sorted(frameworks)}

        return {
            "documents": documents,
            "metadata_store": metadata_store,
            "file_hashes": file_hashes,
            "route_inventory": route_inventory,
            "tech_info": tech_info,
            "dependency_graph": dependency_nodes,
            "indexed_files": indexed_files,
        }

    # -- Action: index / baseline ----------------------------------------------

    def _build_baseline_index(self, repo_paths: List[str]) -> str:
        summaries: List[str] = []
        missing_repos: List[str] = []
        processed_aliases: List[str] = []
        total_files = total_chunks = total_routes = total_high_risk = 0

        for repo_path_str in repo_paths:
            repo_root = Path(repo_path_str)
            if not repo_root.exists() or not repo_root.is_dir():
                missing_repos.append(repo_path_str)
                continue

            alias = self._resolve_repo_alias(repo_root)
            scan = self._scan_single_repo(repo_root, alias)

            if not scan["documents"]:
                summaries.append(f"  - {alias} ({repo_root}): no source files found, skipped")
                continue

            repo_dir = self._repo_context_path(alias)
            repo_dir.mkdir(parents=True, exist_ok=True)

            doc_ids = [doc.metadata["chunk_id"] for doc in scan["documents"]]
            vector_store = FAISS.from_documents(scan["documents"], self._get_embeddings(), ids=doc_ids)
            vector_store.save_local(str(repo_dir / "faiss_index"))

            self._vector_stores[alias] = vector_store
            self._metadata_stores[alias] = scan["metadata_store"]
            self._dependency_graphs[alias] = scan["dependency_graph"]
            self._route_inventories[alias] = scan["route_inventory"]
            self._tech_inventories[alias] = scan["tech_info"]
            self._file_hashes[alias] = scan["file_hashes"]

            self._persist_json(repo_dir / "metadata_store.json", scan["metadata_store"])
            self._persist_json(repo_dir / "blast_radius_graph.json", scan["dependency_graph"])
            self._persist_json(repo_dir / "route_inventory.json", scan["route_inventory"])
            self._persist_json(repo_dir / "tech_inventory.json", scan["tech_info"])
            self._persist_json(repo_dir / "file_hashes.json", scan["file_hashes"])

            high_risk = sum(1 for n in scan["dependency_graph"].values() if n["risk_tags"])
            total_files += scan["indexed_files"]
            total_chunks += len(scan["documents"])
            total_routes += len(scan["route_inventory"])
            total_high_risk += high_risk
            processed_aliases.append(alias)

            summaries.append(
                f"  - {alias} ({repo_root}) -> {repo_dir}: files={scan['indexed_files']}, "
                f"chunks={len(scan['documents'])}, routes={len(scan['route_inventory'])}, high_risk={high_risk}"
            )

        if not processed_aliases:
            missing = f" Missing paths: {missing_repos}." if missing_repos else ""
            return f"[ERROR] No source files found to index across repo_paths={repo_paths}.{missing}"

        header = (
            "[INDEX] Successfully indexed repositories in baseline mode.\n"
            f"Repositories: {', '.join(processed_aliases)}\n"
            f"Files indexed: {total_files}\n"
            f"Chunks embedded: {total_chunks}\n"
            f"Routes discovered: {total_routes}\n"
            f"High-risk modules flagged: {total_high_risk}\n"
            f"Context root: {self._context_path()} (each repo stored in its own named subfolder)\n"
        )
        if missing_repos:
            header += f"Missing paths (skipped): {missing_repos}\n"
        return header + "\n".join(summaries)

    # -- Action: index / diff ---------------------------------------------------

    @staticmethod
    def _resolve_node_ids(graph: Dict[str, Any], paths: List[str]) -> Set[str]:
        resolved: Set[str] = set()
        for raw in paths:
            normalized = raw.replace("\\", "/").strip()
            if not normalized:
                continue
            if normalized in graph:
                resolved.add(normalized)
                continue
            for node_id, node in graph.items():
                if (node["path"] == normalized or node_id.endswith(f"::{normalized}")
                        or normalized.endswith(f"/{node['path']}") or normalized == node["path"]):
                    resolved.add(node_id)
        return resolved

    @staticmethod
    def _blast_radius_node_ids(graph: Dict[str, Any], node_ids: Set[str]) -> Set[str]:
        visited: Set[str] = set()
        queue = deque(node_ids)
        while queue:
            current = queue.popleft()
            node = graph.get(current)
            if not node:
                continue
            for caller in node.get("imported_by", []):
                if caller not in visited and caller not in node_ids:
                    visited.add(caller)
                    queue.append(caller)
        return visited

    def _refresh_diff_index(self, repo_paths: List[str], changed_files: List[str]) -> str:
        if not changed_files:
            return "[ERROR] changed_files parameter is required for diff mode indexing."

        repo_summaries: List[str] = []
        missing_repos: List[str] = []
        load_failures: List[str] = []
        any_repo_matched = False
        total_changed = total_impacted = total_reembedded = total_removed = total_evicted = total_routes_removed = 0
        touched_aliases: List[str] = []

        for repo_path_str in repo_paths:
            repo_root = Path(repo_path_str)
            if not repo_root.exists() or not repo_root.is_dir():
                missing_repos.append(repo_path_str)
                continue

            alias = self._resolve_repo_alias(repo_root)
            load_error = self._ensure_repo_loaded(alias, require_index=True)
            if load_error:
                load_failures.append(load_error)
                continue

            graph = self._dependency_graphs.get(alias, {})
            changed_node_ids = self._resolve_node_ids(graph, changed_files)
            if not changed_node_ids:
                continue  # changed_files don't belong to this repo's graph; skip quietly

            any_repo_matched = True
            touched_aliases.append(alias)
            impacted_node_ids = self._blast_radius_node_ids(graph, changed_node_ids)
            refresh_targets = sorted(changed_node_ids | impacted_node_ids)

            module_map: Dict[str, str] = {}
            for file_path in self._iter_source_files(repo_root):
                if file_path.suffix == ".py":
                    module_map[self._module_name_for(repo_root, file_path)] = self._node_id(alias, repo_root, file_path)

            metadata_store = self._metadata_stores.setdefault(alias, {})
            route_inventory = self._route_inventories.setdefault(alias, [])
            file_hashes = self._file_hashes.setdefault(alias, {})
            vector_store = self._vector_stores.get(alias)

            evicted_chunks = removed_routes = reembedded_files = 0
            removed_files: List[str] = []

            for node_id in refresh_targets:
                node = graph.get(node_id)
                if node is None:
                    continue
                rel_path = node["path"]

                stale_chunk_ids = [cid for cid, meta in metadata_store.items() if meta.get("node_id") == node_id]
                if stale_chunk_ids and vector_store is not None:
                    vector_store.delete(ids=stale_chunk_ids)
                for cid in stale_chunk_ids:
                    metadata_store.pop(cid, None)
                    evicted_chunks += 1

                before = len(route_inventory)
                route_inventory[:] = [r for r in route_inventory if r["file"] != rel_path]
                removed_routes += before - len(route_inventory)

                file_path = repo_root / rel_path
                if not file_path.exists():
                    removed_files.append(node_id)
                    del graph[node_id]
                    file_hashes.pop(node_id, None)
                    continue

                text = file_path.read_text(encoding="utf-8", errors="ignore")
                language = LANGUAGE_BY_EXTENSION.get(file_path.suffix, node["language"])
                file_hashes[node_id] = hashlib.sha256(text.encode("utf-8")).hexdigest()

                risk_tags = self._detect_risk_tags(text, rel_path)
                route_inventory.extend(self._extract_routes(text, rel_path, alias, language))

                imports: Set[str] = set()
                if file_path.suffix == ".py":
                    for candidate in self._extract_python_import_candidates(file_path, repo_root):
                        target = module_map.get(candidate)
                        if target and target != node_id:
                            imports.add(target)

                node["imports"] = sorted(imports)
                node["risk_tags"] = sorted(risk_tags)

                new_docs: List[Document] = []
                for i, chunk in enumerate(self._split_text(text, language)):
                    chunk_id = f"{node_id}#{i}"
                    meta = {
                        "chunk_id": chunk_id, "node_id": node_id, "repo": alias, "path": rel_path,
                        "language": language, "chunk_index": i, "risk_tags": sorted(risk_tags),
                    }
                    metadata_store[chunk_id] = meta
                    new_docs.append(Document(page_content=chunk, metadata=meta))

                if new_docs:
                    new_ids = [doc.metadata["chunk_id"] for doc in new_docs]
                    if vector_store is None:
                        vector_store = FAISS.from_documents(new_docs, self._get_embeddings(), ids=new_ids)
                        self._vector_stores[alias] = vector_store
                    else:
                        vector_store.add_documents(new_docs, ids=new_ids)
                reembedded_files += 1

            for node in graph.values():
                node["imported_by"] = []
            for node_id, node in graph.items():
                for dep in node["imports"]:
                    if dep in graph:
                        graph[dep]["imported_by"].append(node_id)
            for node in graph.values():
                node["imported_by"] = sorted(set(node["imported_by"]))

            repo_dir = self._repo_context_path(alias)
            if vector_store is not None:
                vector_store.save_local(str(repo_dir / "faiss_index"))
            self._persist_json(repo_dir / "metadata_store.json", metadata_store)
            self._persist_json(repo_dir / "blast_radius_graph.json", graph)
            self._persist_json(repo_dir / "route_inventory.json", route_inventory)
            self._persist_json(repo_dir / "file_hashes.json", file_hashes)

            total_changed += len(changed_node_ids)
            total_impacted += len(impacted_node_ids)
            total_reembedded += reembedded_files
            total_removed += len(removed_files)
            total_evicted += evicted_chunks
            total_routes_removed += removed_routes

            repo_summaries.append(
                f"  - {alias}: changed={len(changed_node_ids)}, blast_radius_impacted={len(impacted_node_ids)}, "
                f"re-embedded={reembedded_files}, removed={len(removed_files)}, chunks_evicted={evicted_chunks}, "
                f"routes_removed={removed_routes}"
            )

        if not any_repo_matched:
            if load_failures:
                return load_failures[0]
            return (
                f"[ERROR] None of the provided changed_files could be matched to an indexed repo: {changed_files}. "
                "Run a baseline index first, or verify repo_paths/changed_files are correct."
            )

        header = (
            "[INDEX] Successfully refreshed index in diff mode.\n"
            f"Repositories touched: {', '.join(touched_aliases)}\n"
            f"Changed files: {total_changed}\n"
            f"Blast-radius impacted files also refreshed: {total_impacted}\n"
            f"Files re-embedded: {total_reembedded}\n"
            f"Files removed (deleted on disk): {total_removed}\n"
            f"Chunks evicted: {total_evicted}\n"
            f"Route entries removed: {total_routes_removed}\n"
        )
        if missing_repos:
            header += f"Missing repo paths (skipped): {missing_repos}\n"
        return header + "\n".join(repo_summaries)

    # -- Action: search ----------------------------------------------------------

    def _perform_semantic_search(self, query: str, top_k: Optional[int], repo_paths: Optional[List[str]] = None) -> str:
        requested = self._requested_aliases(repo_paths or [])
        aliases = requested or self._discover_persisted_aliases()
        if not aliases:
            return "[ERROR] No persisted index found. Run action='index' with mode='baseline' first."

        k = top_k or 5
        all_results = []
        load_errors: List[str] = []
        for alias in aliases:
            error = self._ensure_repo_loaded(alias, require_index=True)
            if error:
                load_errors.append(error)
                continue
            store = self._vector_stores.get(alias)
            if store is None:
                continue
            all_results.extend(store.similarity_search_with_score(query, k=k))

        if not all_results:
            if load_errors and len(load_errors) == len(aliases):
                return load_errors[0]
            return f"[SEARCH] No relevant results found for query: '{query}'"

        all_results.sort(key=lambda pair: pair[1])
        top_results = all_results[:k]

        blocks = [f"[SEARCH] Top {len(top_results)} result(s) for query: '{query}' (searched repos: {', '.join(aliases)})"]
        for rank, (doc, score) in enumerate(top_results, start=1):
            meta = doc.metadata
            snippet = doc.page_content.strip()
            if len(snippet) > 400:
                snippet = snippet[:400] + "..."
            risk_tags = meta.get("risk_tags") or []
            risk_suffix = f", risk={','.join(risk_tags)}" if risk_tags else ""
            blocks.append(
                f"{rank}. [{meta.get('repo')}] {meta.get('path')} "
                f"(chunk {meta.get('chunk_index')}, score={score:.4f}{risk_suffix})\n"
                f"```\n{snippet}\n```"
            )
        return "\n\n".join(blocks)

    # -- Action: get_inventory ------------------------------------------------

    def _generate_inventory(self, repo_paths: List[str]) -> str:
        repo_reports: List[tuple] = []
        missing_repos: List[str] = []
        total_files = 0

        for repo_path_str in repo_paths:
            repo_root = Path(repo_path_str)
            if not repo_root.exists() or not repo_root.is_dir():
                missing_repos.append(repo_path_str)
                continue

            alias = self._resolve_repo_alias(repo_root)
            scan = self._scan_single_repo(repo_root, alias)

            repo_dir = self._repo_context_path(alias)
            repo_dir.mkdir(parents=True, exist_ok=True)
            self._dependency_graphs[alias] = scan["dependency_graph"]
            self._route_inventories[alias] = scan["route_inventory"]
            self._tech_inventories[alias] = scan["tech_info"]
            self._file_hashes[alias] = scan["file_hashes"]
            self._persist_json(repo_dir / "blast_radius_graph.json", scan["dependency_graph"])
            self._persist_json(repo_dir / "route_inventory.json", scan["route_inventory"])
            self._persist_json(repo_dir / "tech_inventory.json", scan["tech_info"])
            self._persist_json(repo_dir / "file_hashes.json", scan["file_hashes"])

            total_files += scan["indexed_files"]
            repo_reports.append((alias, scan))

        if not repo_reports:
            missing = f" Missing paths: {missing_repos}." if missing_repos else ""
            return f"[ERROR] No repositories could be scanned for repo_paths={repo_paths}.{missing}"

        lines = ["# Multi-Repo Technology & Route Inventory", "", "## Technology Inventory"]
        for alias, scan in repo_reports:
            info = scan["tech_info"]
            lang_summary = ", ".join(f"{lang} ({count})" for lang, count in sorted(info["languages"].items()))
            fw_summary = ", ".join(info["frameworks"]) or "none detected"
            lines.append(f"- **{alias}** ({info['path']}): languages: {lang_summary or 'none'}; frameworks: {fw_summary}")

        lines.append("")
        lines.append("## Route Inventory")
        all_routes = [r for _, scan in repo_reports for r in scan["route_inventory"]]
        if all_routes:
            lines.append("| Method | Path | Handler File | Repo |")
            lines.append("|---|---|---|---|")
            for route in sorted(all_routes, key=lambda r: (r["repo"], r["path"])):
                lines.append(f"| {route['method']} | {route['path']} | {route['file']} | {route['repo']} |")
        else:
            lines.append("No routes discovered.")

        lines.append("")
        lines.append("## High-Risk Modules")
        all_high_risk = [n for _, scan in repo_reports for n in scan["dependency_graph"].values() if n["risk_tags"]]
        if all_high_risk:
            for node in sorted(all_high_risk, key=lambda n: (n["repo"], n["path"])):
                lines.append(f"- [{'/'.join(node['risk_tags'])}] {node['repo']}::{node['path']}")
        else:
            lines.append("No high-risk modules flagged.")

        lines.append("")
        lines.append(f"Files scanned: {total_files}")
        lines.append(f"Context root: {self._context_path()} (subfolders: {', '.join(a for a, _ in repo_reports)})")
        if missing_repos:
            lines.append(f"Missing repo paths: {missing_repos}")

        return "\n".join(lines)

    # -- Action: get_blast_radius ----------------------------------------------

    def _calculate_blast_radius(self, changed_files: List[str], repo_paths: Optional[List[str]] = None) -> str:
        requested = self._requested_aliases(repo_paths or [])
        aliases = requested or self._discover_persisted_aliases()
        if not aliases:
            return (
                "[ERROR] No blast radius graph available. Run action='index' with mode='baseline' first "
                f"to build the dependency graph. (changed_files={changed_files})"
            )

        merged_graph: Dict[str, Any] = {}
        for alias in aliases:
            self._ensure_repo_loaded(alias, require_index=False)
            merged_graph.update(self._dependency_graphs.get(alias, {}))

        if not merged_graph:
            return (
                "[ERROR] No blast radius graph available. Run action='index' with mode='baseline' first "
                f"to build the dependency graph. (changed_files={changed_files})"
            )

        changed_node_ids = self._resolve_node_ids(merged_graph, changed_files)
        resolved_paths = {merged_graph[nid]["path"] for nid in changed_node_ids if nid in merged_graph}
        unresolved = [
            f for f in changed_files
            if f.replace("\\", "/") not in changed_node_ids
            and f.replace("\\", "/") not in resolved_paths
            and not any(f.replace("\\", "/").endswith(f"/{p}") for p in resolved_paths)
        ]

        direct_impact: Set[str] = set()
        for nid in changed_node_ids:
            direct_impact.update(merged_graph.get(nid, {}).get("imported_by", []))

        transitive_impact = self._blast_radius_node_ids(merged_graph, changed_node_ids) - direct_impact - changed_node_ids

        upstream_deps: Set[str] = set()
        for nid in changed_node_ids:
            upstream_deps.update(merged_graph.get(nid, {}).get("imports", []))

        def describe(nid: str) -> str:
            node = merged_graph.get(nid, {})
            return f"{node.get('repo', '?')}::{node.get('path', nid)}"

        lines = [
            f"[BLAST RADIUS] Impact analysis for {len(changed_files)} changed file(s) "
            f"(searched repos: {', '.join(aliases)}).",
            "", "Changed files:",
        ]
        lines.extend(f"  - {f}" for f in changed_files)

        if unresolved:
            lines.append("")
            lines.append("Unresolved (not found in dependency graph - new/untracked files):")
            lines.extend(f"  - {f}" for f in unresolved)

        lines.append("")
        lines.append(f"Directly impacted callers ({len(direct_impact)}):")
        if direct_impact:
            lines.extend(f"  - {describe(nid)}" for nid in sorted(direct_impact))
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append(f"Transitively impacted callers ({len(transitive_impact)}):")
        if transitive_impact:
            lines.extend(f"  - {describe(nid)}" for nid in sorted(transitive_impact))
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append(f"Upstream dependencies of changed files ({len(upstream_deps)}):")
        if upstream_deps:
            lines.extend(f"  - {describe(nid)}" for nid in sorted(upstream_deps))
        else:
            lines.append("  (none)")

        total_impacted = len(changed_node_ids | direct_impact | transitive_impact)
        lines.append("")
        lines.append(f"Total files in blast radius (changed + impacted): {total_impacted}")

        return "\n".join(lines)
