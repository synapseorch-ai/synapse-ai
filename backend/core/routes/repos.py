"""
Repo management endpoints (CRUD + reindex).
"""
import os
import json
from fastapi import APIRouter, HTTPException, BackgroundTasks
from core.models import Repo
from core.config import DATA_DIR, load_settings
from core.json_store import JsonStore

router = APIRouter()

_repos_store = JsonStore(os.path.join(DATA_DIR, "repos.json"))


def load_repos() -> list[dict]:
    return _repos_store.load()


def save_repos(repos: list[dict]):
    _repos_store.save(repos)

@router.get("/api/repos")
async def get_repos():
    repos = load_repos()

    if load_settings().get("embed_code", False):
        try:
            from services.code_indexer import get_index_status, _active_threads
            updated = False
            for r in repos:
                if r.get("status") in ("indexing", "stopping"):
                    # Check whether the background thread is still alive.
                    # After a backend restart _active_threads is empty, so any
                    # repo left in "indexing" / "stopping" is a stale artifact.
                    thread = _active_threads.get(r["id"])
                    if not thread or not thread.is_alive():
                        r["status"] = "stopped"
                        updated = True
                    else:
                        # Thread alive — just refresh the live chunk count
                        stats = get_index_status(r["id"])
                        if stats["count"] != r.get("file_count", 0):
                            r["file_count"] = stats["count"]
                            updated = True
                    continue

                stats = get_index_status(r["id"])
                if stats["status"] == "indexed":
                    if r.get("status") != "indexed" or r.get("file_count") != stats["count"]:
                        r["status"] = "indexed"
                        r["file_count"] = stats["count"]
                        updated = True
                elif stats["status"] == "error":
                    if r.get("status") != "error":
                        r["status"] = "error"
                        updated = True
            if updated:
                save_repos(repos)
        except ImportError:
            pass
    else:
        # embed_code is off — reset any repos previously poisoned with "error"
        # (from a failed DB connection) back to "pending".
        updated = False
        for r in repos:
            if r.get("status") == "error":
                r["status"] = "pending"
                updated = True
        if updated:
            save_repos(repos)

    return repos

@router.post("/api/repos")
async def create_repo(repo: Repo, background_tasks: BackgroundTasks):
    repos = load_repos()
    for i, r in enumerate(repos):
        if r["id"] == repo.id:
            repos[i] = repo.dict()
            save_repos(repos)
            try:
                import core.server as _server
                await _server.restart_filesystem_mcp()
            except Exception as e:
                print(f"Warning: Failed to restart filesystem MCP after repo update: {e}")
            return repo

    repos.append(repo.dict())
    save_repos(repos)

    try:
        import core.server as _server
        await _server.restart_filesystem_mcp()
    except Exception as e:
        print(f"Warning: Failed to restart filesystem MCP after adding repo: {e}")

    # Auto-index new repos if path exists and embed_code is enabled
    real_path = os.path.realpath(repo.path)
    if os.path.isdir(real_path) and load_settings().get("embed_code", False):
        try:
            from services.code_indexer import run_index
            for r in repos:
                if r["id"] == repo.id:
                    r["status"] = "indexing"
                    break
            save_repos(repos)
            background_tasks.add_task(run_index, repo.id, real_path, repo.included_patterns, repo.excluded_patterns)
        except ImportError:
            pass

    return repo

@router.delete("/api/repos/{repo_id}")
async def delete_repo(repo_id: str):
    repos = load_repos()
    repos = [r for r in repos if r["id"] != repo_id]
    save_repos(repos)

    # Remove deleted repo from all agents' repos list
    try:
        from core.routes.agents import load_user_agents, save_user_agents
        agents = load_user_agents()
        modified = False
        for agent in agents:
            if repo_id in agent.get("repos", []):
                agent["repos"] = [r for r in agent["repos"] if r != repo_id]
                modified = True
        if modified:
            save_user_agents(agents)
            print(f"Removed repo {repo_id} from agents.")
    except Exception as e:
        print(f"Warning: Failed to remove repo {repo_id} from agents: {e}")

    # Restart filesystem MCP to drop the deleted repo path
    try:
        import core.server as _server
        await _server.restart_filesystem_mcp()
    except Exception as e:
        print(f"Warning: Failed to restart filesystem MCP after deleting repo: {e}")

    try:
        from services.code_indexer import drop_index
        drop_index(repo_id)
    except Exception as e:
        print(f"Error dropping index for repo {repo_id}: {e}")
    return {"status": "success"}

@router.post("/api/repos/{repo_id}/reindex")
async def reindex_repo(repo_id: str, background_tasks: BackgroundTasks):
    repos = load_repos()
    repo = next((r for r in repos if r["id"] == repo_id), None)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not load_settings().get("embed_code", False):
        raise HTTPException(status_code=400, detail="Code indexing is disabled (embed_code=false)")

    if repo.get("status") == "indexing":
        raise HTTPException(status_code=409, detail="Repo is already being indexed")

    real_path = os.path.realpath(repo.get("path", ""))
    if not os.path.isdir(real_path):
        raise HTTPException(status_code=400, detail=f"Repo path does not exist: {repo.get('path')}")

    # Set status
    repo["status"] = "indexing"
    repo["error_message"] = None  # Clear any previous error
    for i, r in enumerate(repos):
        if r["id"] == repo_id:
            repos[i] = repo
            break
    save_repos(repos)

    # Run in background
    try:
        from services.code_indexer import run_index, COCOINDEX_AVAILABLE, _coco_import_error
        if not COCOINDEX_AVAILABLE:
            detail = _coco_import_error or "unknown import error"
            repo["status"] = "error"
            repo["error_message"] = (
                f"CocoIndex dependency error: {detail}. "
                f"Install the correct version into the backend venv: "
                f"pip install 'cocoindex>=0.3.30,<1.0' psycopg, then restart Synapse."
            )
            save_repos(repos)
            raise HTTPException(
                status_code=500,
                detail=f"CocoIndex not available: {detail}. "
                       f"Install 'cocoindex>=0.3.30,<1.0' and psycopg into the backend venv, then restart."
            )
        background_tasks.add_task(run_index, repo_id, real_path, repo["included_patterns"], repo["excluded_patterns"])
    except HTTPException:
        raise
    except ImportError as e:
        print("Indexer unavailable:", e)
        repo["status"] = "error"
        repo["error_message"] = f"Indexer import failed: {e}"
        save_repos(repos)
        raise HTTPException(status_code=500, detail=f"Indexer service not available: {e}")

    return {"status": "indexing_started"}


@router.post("/api/repos/{repo_id}/stop-index")
async def stop_index_repo(repo_id: str):
    repos = load_repos()
    repo = next((r for r in repos if r["id"] == repo_id), None)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if repo.get("status") not in ("indexing", "stopping"):
        raise HTTPException(status_code=409, detail="Repo is not currently indexing")

    try:
        from services.code_indexer import stop_index
        was_running = stop_index(repo_id)
    except ImportError:
        raise HTTPException(status_code=500, detail="Indexer service not available")

    # If a live thread was signalled, mark "stopping" (thread will set "stopped" when done).
    # If no thread is alive (e.g. after a backend restart), mark "stopped" immediately.
    new_status = "stopping" if was_running else "stopped"
    for i, r in enumerate(repos):
        if r["id"] == repo_id:
            repos[i]["status"] = new_status
            break
    save_repos(repos)

    return {"status": new_status, "was_running": was_running}
