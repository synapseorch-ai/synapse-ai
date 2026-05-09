"""
Code indexer service: CocoIndex flow definitions, index management, and DB operations.
Handles repo indexing lifecycle — creation, status, deletion, and background indexing.
"""

import asyncio
import json
import os
import threading
import traceback
from datetime import datetime
from typing import Annotated, Any
from core.config import load_settings, sanitize_db_url

try:
    import cocoindex
    import psycopg
    COCOINDEX_AVAILABLE = True
    import numpy as np
    from numpy.typing import NDArray
    from cocoindex.typing import VectorInfo
except ImportError as _coco_import_err:
    COCOINDEX_AVAILABLE = False
    NDArray = None
    VectorInfo = None
    print(
        f"[code-indexer] WARNING: cocoindex/psycopg not found in this Python environment ({_coco_import_err}).\n"
        "  Code indexing is disabled. To enable it, install into the backend venv:\n"
        "    <synapse-install-dir>/backend/venv/bin/pip install cocoindex psycopg\n"
        "  Then restart Synapse."
    )

# Lock for repos.json read/write
_repos_lock = threading.Lock()

# Cache of active CocoIndex Flow objects
_active_flows: dict[str, object] = {}

# Cache of registered embedding ops, keyed by (model_name, dimension)
_embedding_fn_cache: dict[tuple[str, int], Any] = {}

# Per-repo stop signals — set to request cancellation of an active index task
_stop_events: dict[str, threading.Event] = {}
# Active index threads so we can check is_alive()
_active_threads: dict[str, threading.Thread] = {}

def _get_db_url() -> str:
    """Always read the database URL from settings.json (never from the environment).
    Sanitizes the URL so that an empty password (e.g. user:@host) is rewritten
    to the form psycopg can parse (user@host)."""
    raw = load_settings().get("sql_connection_string", "")
    return sanitize_db_url(raw)

# Known output dimensions for popular embedding models.
# Only base model IDs are listed here — provisioned-throughput suffixes
# (e.g. ":2:8k", ":1:512k") are handled by the prefix-match fallback in
# probe_embedding_dim(), so there is no need to enumerate every variant.
EMBEDDING_MODEL_DIMS: dict[str, int] = {
    # Gemini
    "gemini-embedding-001": 768,
    "text-embedding-004": 768,
    "gemini-embedding-004": 768,
    # OpenAI
    "text-embedding-ada-002": 1536,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    # Bedrock — Titan Text Embeddings
    "bedrock.amazon.titan-embed-text-v1": 1536,
    "bedrock.amazon.titan-embed-text-v2:0": 1024,
    # Bedrock — Cohere
    "bedrock.cohere.embed-english-v3": 1024,
    "bedrock.cohere.embed-multilingual-v3": 1024,
    # Ollama popular models
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
}

# These patterns are ALWAYS excluded, merged with any user-supplied list.
# ALL directory patterns use **/name/** so they match at ANY depth in the tree.
# Without the **/ prefix, CocoIndex matches only against the root-level name.
BASE_EXCLUDED_PATTERNS: list[str] = [
    # --- Version control ---
    "**/.git/**", "**/.svn/**", "**/.hg/**",
    # --- Python virtualenvs & caches ---
    "**/venv/**", "**/.venv/**",
    "**/env/**",  "**/.env/**",
    "**/__pycache__/**",
    "**/*.pyc", "**/*.pyo", "**/*.pyd",
    "**/.eggs/**", "**/*.egg-info/**", "**/*.egg",
    "**/site-packages/**",              # matches both inside venv/lib and system
    "**/.mypy_cache/**", "**/.ruff_cache/**",
    "**/.pytest_cache/**", "**/.tox/**",
    # --- Node.js / JavaScript ---
    "**/node_modules/**",
    "**/.next/**", "**/.nuxt/**", "**/.svelte-kit/**",
    "**/dist/**", "**/build/**", "**/out/**", "**/output/**",
    "**/.cache/**", "**/.parcel-cache/**", "**/.turbo/**",
    # --- Package managers ---
    "**/vendor/**",                     # Go, PHP (Composer)
    "**/bower_components/**",
    "**/.bundle/**",                    # Ruby bundler
    "**/Pods/**",                       # CocoaPods
    # --- IDE / Editor ---
    "**/.idea/**", "**/.vscode/**", "**/.vs/**",
    # --- Build artefacts ---
    "**/target/**",                     # Rust / Maven
    "**/*.min.js", "**/*.min.css",
    "**/*.map",                         # source-maps
    "**/*.class",                       # Java
    "**/*.o", "**/*.so", "**/*.dll", "**/*.dylib",  # compiled objects
    "**/*.jar", "**/*.war", "**/*.ear",
    # --- Test / coverage ---
    "**/coverage/**", "**/htmlcov/**", "**/.nyc_output/**",
    "**/__snapshots__/**",
    # --- Lock files (large, near-zero semantic value) ---
    "**/*.lock",
    # --- Logs & temp ---
    "**/*.log", "**/*.tmp", "**/*.temp",
    # --- OS noise ---
    "**/.DS_Store", "**/Thumbs.db",
]


def get_configured_embedding_model() -> str:
    """Load and validate the configured embedding model. Raises if not set."""
    settings = load_settings()
    model = (settings.get("embedding_model") or "").strip()
    if not model:
        raise ValueError(
            "Embedding model is not configured. "
            "Please set an embedding model in Settings → Models before indexing."
        )
    return model


def probe_embedding_dim(model: str, settings: dict) -> int:
    """Return the embedding output dimension for *model*.

    For known models the lookup table is used directly — this avoids calling
    the API with no output_dimensionality constraint (which can return the full
    Matryoshka size, e.g. 3072 for Gemini, which exceeds pgvector's HNSW limit).
    For unknown models a live probe is made.
    """
    # Prefer the lookup table for known models (safe, no API call needed)
    known_dim = EMBEDDING_MODEL_DIMS.get(model)
    if known_dim is not None:
        print(f"[index] Resolved dim for '{model}' from lookup table: {known_dim}")
        return known_dim

    # Prefix-match fallback: handles provisioned-throughput suffixes like ":2:8k"
    # that Bedrock appends to base model IDs.  Pick the longest matching prefix.
    prefix_match = None
    prefix_len = 0
    for known_model, dim in EMBEDDING_MODEL_DIMS.items():
        if model.startswith(known_model) and len(known_model) > prefix_len:
            prefix_match = (known_model, dim)
            prefix_len = len(known_model)
    if prefix_match is not None:
        matched_model, matched_dim = prefix_match
        print(f"[index] Resolved dim for '{model}' via prefix match on '{matched_model}': {matched_dim}")
        return matched_dim

    # Unknown model — probe by making one test call
    from core.llm_providers import embed_batch
    try:
        probe_settings = dict(settings)
        new_loop = asyncio.new_event_loop()
        try:
            result = new_loop.run_until_complete(embed_batch(["dimension probe"], model, probe_settings))
        finally:
            new_loop.close()
        if result and len(result[0]) > 0:
            dim = len(result[0])
            print(f"[index] Probed embedding dim for '{model}': {dim}")
            return dim
    except Exception as e:
        print(f"[index] Warning: probe failed ({e})")

    raise ValueError(
        f"Unknown embedding model '{model}' and dimension probe failed. "
        f"Add it to EMBEDDING_MODEL_DIMS in code_indexer.py or use a known model."
    )


def get_embedding_fn(model: str, dim: int):
    """Return (and cache) the cocoindex-registered embedding op for the given model+dim."""
    from core.llm_providers import embed_batch

    cache_key = (model, dim)
    if cache_key in _embedding_fn_cache:
        return _embedding_fn_cache[cache_key]

    # The return type annotation must specify the exact dimension so CocoIndex
    # creates a vector(N) pgvector column instead of falling back to jsonb.
    vector_type = Annotated[NDArray[np.float32], VectorInfo(dim=dim)]
    fn_name = f"embed_{model.replace('-', '_').replace('.', '_')}_{dim}d"

    def _impl(texts: list[str]) -> list[vector_type]:
        cfg = load_settings()
        current_model = (cfg.get("embedding_model") or "").strip() or model
        # Tell the provider (Gemini) to return exactly `dim` dimensions so we
        # never store more floats than the pgvector column was created for.
        cfg["__embed_output_dim"] = dim
        try:
            new_loop = asyncio.new_event_loop()
            try:
                embeddings = new_loop.run_until_complete(embed_batch(texts, current_model, cfg))
            finally:
                new_loop.close()
        except Exception as e:
            print(f"ERROR: embed call failed: {e}")
            traceback.print_exc()
            embeddings = [[0.0] * dim for _ in range(len(texts))]
        # Safety-truncate to `dim` in case any provider returns extra dims
        return [np.array(e[:dim], dtype=np.float32) for e in embeddings]

    _impl.__name__ = fn_name
    _impl.__qualname__ = fn_name

    if COCOINDEX_AVAILABLE:
        try:
            fn = cocoindex.op.function(batching=True, max_batch_size=100)(_impl)
        except RuntimeError:
            # Already registered under this name — reuse cached plain impl.
            # CocoIndex uses the previously-registered schema for functions with
            # the same name, so this is safe as long as dim hasn't changed.
            fn = _impl
    else:
        fn = _impl

    _embedding_fn_cache[cache_key] = fn
    return fn


def get_table_name(repo_id: str) -> str:
    return f"ci_{repo_id}__emb"


def create_repo_flow(repo_id: str, repo_path: str, included_patterns: list[str], excluded_patterns: list[str]):
    """Dynamically create a CocoIndex flow for a specific repo."""
    if not COCOINDEX_AVAILABLE:
        raise RuntimeError("CocoIndex is not installed.")

    # Merge user patterns with the always-on base exclusions (deduped)
    effective_excluded = sorted(set(BASE_EXCLUDED_PATTERNS) | set(excluded_patterns))
    print(f"[index] Excluded patterns ({len(effective_excluded)}): {effective_excluded}")

    # Validate and resolve embedding model + dimension
    model = get_configured_embedding_model()
    settings = load_settings()
    dim = probe_embedding_dim(model, settings)
    get_embeddings = get_embedding_fn(model, dim)
    print(f"[index] Embedding model='{model}' dim={dim}")

    flow_name = f"ci_{repo_id}"
    if flow_name in _active_flows:
        try:
            _active_flows[flow_name].close()
        except Exception:
            pass
        del _active_flows[flow_name]

    @cocoindex.flow_def(name=flow_name)
    def repo_flow(flow_builder: cocoindex.FlowBuilder, data_scope: cocoindex.DataScope):
        data_scope["files"] = flow_builder.add_source(
            cocoindex.sources.LocalFile(
                path=repo_path,
                included_patterns=included_patterns,
                excluded_patterns=effective_excluded,
            )
        )
        code_embeddings = data_scope.add_collector()

        with data_scope["files"].row() as file:
            file["language"] = file["filename"].transform(
                cocoindex.functions.DetectProgrammingLanguage()
            )
            file["chunks"] = file["content"].transform(
                cocoindex.functions.SplitRecursively(),
                language=file["language"],
                chunk_size=1000,
                min_chunk_size=300,
                chunk_overlap=300,
            )
            with file["chunks"].row() as chunk:
                chunk["embedding"] = chunk["text"].transform(get_embeddings)
                code_embeddings.collect(
                    filename=file["filename"],
                    location=chunk["location"],
                    code=chunk["text"],
                    embedding=chunk["embedding"],
                )

        code_embeddings.export(
            "emb",
            cocoindex.targets.Postgres(),
            primary_key_fields=["filename", "location"],
            vector_indexes=[
                cocoindex.VectorIndexDef(
                    field_name="embedding",
                    metric=cocoindex.VectorSimilarityMetric.COSINE_SIMILARITY,
                )
            ],
        )

    _active_flows[flow_name] = repo_flow
    return repo_flow


def _ensure_database_exists():
    if not COCOINDEX_AVAILABLE:
        return
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(_get_db_url())
    db_name = parsed.path.lstrip("/")
    admin_url = urlunparse(parsed._replace(path="/postgres"))
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{db_name}"')
                    print(f"Created database '{db_name}'.")
    except Exception as e:
        print(f"Warning: could not ensure database exists: {e}")


def init_cocoindex():
    if not COCOINDEX_AVAILABLE:
        return
    _ensure_database_exists()
    os.environ["COCOINDEX_DATABASE_URL"] = _get_db_url()
    print("CocoIndex init check done.")


def get_index_status(repo_id: str) -> dict:
    if not COCOINDEX_AVAILABLE:
        return {"status": "unavailable", "count": 0}
    db_url = _get_db_url()
    if not db_url:
        return {"status": "unavailable", "count": 0}
    table_name = get_table_name(repo_id)
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s);", (f"public.{table_name}",))
                if not cur.fetchone()[0]:
                    return {"status": "pending", "count": 0}
                cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
                count = cur.fetchone()[0]
                return {"status": "indexed", "count": count}
    except Exception as e:
        print(f"Error checking index {repo_id}: {e}")
        return {"status": "error", "message": str(e), "count": 0}


def drop_index(repo_id: str):
    """Drop all tables and CocoIndex metadata for a repo — ensures clean rebuild."""
    if not COCOINDEX_AVAILABLE:
        return
    table_name = get_table_name(repo_id)
    tracking = f"ci_{repo_id}__cocoindex_tracking"
    flow_name = f"ci_{repo_id}"
    try:
        with psycopg.connect(_get_db_url()) as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')
                cur.execute(f'DROP TABLE IF EXISTS "{tracking}" CASCADE;')
                # cocoindex_setup_metadata is created lazily by cocoindex.init() —
                # it won't exist on a fresh installation.  Use a SAVEPOINT so a
                # missing table is a silent no-op that doesn't affect the DROPs above.
                cur.execute("SAVEPOINT before_meta_delete;")
                try:
                    cur.execute(
                        "DELETE FROM cocoindex_setup_metadata WHERE flow_name = %s;",
                        (flow_name,),
                    )
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT before_meta_delete;")
            conn.commit()
    except Exception as e:
        print(f"Warning during drop_index({repo_id}): {e}")


def _update_repo_status(repo_id: str, **fields):
    repos_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "repos.json")
    if not os.path.exists(repos_file):
        return
    with _repos_lock:
        with open(repos_file, "r") as f:
            repos = json.load(f)
        for r in repos:
            if r["id"] == repo_id:
                r.update(fields)
                break
        with open(repos_file, "w") as f:
            json.dump(repos, f, indent=4)


def run_index_task(repo_id: str, repo_path: str, included_patterns: list[str], excluded_patterns: list[str]):
    if not COCOINDEX_AVAILABLE:
        msg = (
            "CocoIndex not installed in the backend venv — indexing skipped.\n"
            "Fix: run   <synapse-install-dir>/backend/venv/bin/pip install cocoindex psycopg\n"
            "then restart Synapse.  (Running bare 'pip install cocoindex' installs into\n"
            "your system Python, not the venv the backend uses.)"
        )
        print(msg)
        _update_repo_status(repo_id, status="error", error_message=
            "cocoindex not installed in backend venv. "
            "Run: <install-dir>/backend/venv/bin/pip install cocoindex psycopg, then restart.")
        return

    stop = _stop_events.setdefault(repo_id, threading.Event())
    stop.clear()  # reset from any previous stop request

    print(f"Starting index builder for {repo_id}...")
    _update_repo_status(repo_id, status="indexing", error_message=None)
    try:
        print("[index] Step 0: drop stale tables + CocoIndex metadata")
        drop_index(repo_id)
        if stop.is_set():
            print(f"[index] Stop requested after step 0 — aborting {repo_id}")
            _update_repo_status(repo_id, status="stopped", error_message=None)
            return

        print("[index] Step 1: create_repo_flow")
        repo_flow = create_repo_flow(repo_id, repo_path, included_patterns, excluded_patterns)
        if stop.is_set():
            print(f"[index] Stop requested after step 1 — aborting {repo_id}")
            _update_repo_status(repo_id, status="stopped", error_message=None)
            return

        print("[index] Step 2: cocoindex.init()")
        os.environ["COCOINDEX_DATABASE_URL"] = _get_db_url()
        cocoindex.init()
        if stop.is_set():
            print(f"[index] Stop requested after step 2 — aborting {repo_id}")
            _update_repo_status(repo_id, status="stopped", error_message=None)
            return

        print("[index] Step 3: repo_flow.setup()")
        repo_flow.setup()
        if stop.is_set():
            print(f"[index] Stop requested after step 3 — aborting {repo_id}")
            _update_repo_status(repo_id, status="stopped", error_message=None)
            return

        # update() is a long-running Rust call — we can't interrupt it mid-way,
        # but we check the stop flag immediately after it returns.
        print("[index] Step 4: repo_flow.update()")
        repo_flow.update(full_reprocess=True)

        if stop.is_set():
            print(f"[index] Stop requested — marking {repo_id} as stopped")
            stats = get_index_status(repo_id)
            _update_repo_status(repo_id, status="stopped",
                                file_count=stats["count"], error_message=None)
            return

        stats = get_index_status(repo_id)
        _update_repo_status(
            repo_id,
            status=stats["status"],
            file_count=stats["count"],
            last_indexed=datetime.now().isoformat(),
            error_message=None,
        )
        print(f"Finished index builder for {repo_id}. Rows indexed: {stats.get('count', 0)}")

    except Exception as e:
        if stop.is_set():
            print(f"[index] Stopped (with exception) for {repo_id}: {e}")
            _update_repo_status(repo_id, status="stopped", error_message=None)
        else:
            print(f"Error running index builder for {repo_id}: {e}")
            traceback.print_exc()
            _update_repo_status(repo_id, status="error", error_message=str(e)[:500])
    finally:
        _active_threads.pop(repo_id, None)


def stop_index(repo_id: str) -> bool:
    """Signal the active index task for *repo_id* to stop. Returns True if a task was running."""
    event = _stop_events.get(repo_id)
    thread = _active_threads.get(repo_id)
    if thread and thread.is_alive():
        if event:
            event.set()
        return True
    return False


def run_index(repo_id: str, repo_path: str, included_patterns: list[str], excluded_patterns: list[str]):
    t = threading.Thread(
        target=run_index_task,
        args=(repo_id, repo_path, included_patterns, excluded_patterns),
        daemon=True,
    )
    _active_threads[repo_id] = t
    t.start()
