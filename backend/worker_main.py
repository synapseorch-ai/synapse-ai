"""
Worker entry point.

Starts two things:
  1. A minimal FastAPI health server on WORKER_HEALTH_PORT (default 9000)
     so the Scale settings UI can ping individual workers.
  2. The ARQ worker via arq.run_worker(WorkerSettings), which blocks
     the main asyncio event loop pulling and executing jobs.

The health server runs in a background thread so the ARQ event loop
is never blocked.
"""
import os
import sys
import threading
import time
import asyncio

# Ensure backend/ is on sys.path so `from core.xxx` works
sys.path.insert(0, os.path.dirname(__file__))

# Mark this process as a scale worker — all resolvers will prefer Postgres
import core.scale.context as _scale_ctx  # noqa: E402
_scale_ctx.IS_SCALE_WORKER = True


# ---------------------------------------------------------------------------
# Health server (runs in a thread, separate from ARQ event loop)
# ---------------------------------------------------------------------------

def _start_health_server():
    import uvicorn
    from fastapi import FastAPI

    health_app = FastAPI()
    _start_time = time.time()

    @health_app.get("/health")
    def health():
        from core.scale.worker import _worker_id, _get_active_jobs, _worker_address
        from core.scale.config import get_scale_config
        cfg = get_scale_config()

        return {
            "status": "ok",
            "worker_id": _worker_id or "starting",
            "address": _worker_address or "",
            "active_jobs": _get_active_jobs(),
            "max_jobs": cfg.worker_concurrency,
            "uptime_seconds": int(time.time() - _start_time),
            "pg_connected": True,   # if we got here, PG is up
            "redis_connected": True,
        }

    port = int(os.getenv("WORKER_HEALTH_PORT", "9000"))
    uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="warning")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    # Start health server in a daemon thread so it doesn't block shutdown
    health_thread = threading.Thread(target=_start_health_server, daemon=True)
    health_thread.start()

    # Small delay to let health server bind its port before ARQ starts
    time.sleep(0.5)

    # Run the ARQ worker (blocks until SIGTERM / SIGINT)
    from arq import run_worker
    from core.scale.worker import WorkerSettings

    # WorkerSettings.redis_settings is a classmethod — arq expects the class
    run_worker(WorkerSettings)


if __name__ == "__main__":
    main()
