"""Background worker process: ``python -m pitchforce worker``.

Runs separately from the web process so long media work never depends on a web request.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from ..config import get_settings
from ..db import SessionLocal
from ..models import ProcessingJob
from . import queue
from .queue import HANDLERS, JobCancelled, JobContext, PermanentJobError

log = logging.getLogger("pitchforce.worker")


def run_one(job_id: str, worker_id: str) -> str:
    """Execute one claimed job. Returns final status string."""
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        ctx = JobContext(job.id, worker_id, job.kind, dict(job.payload or {}), job.event_id, job.company_id, job.attempts)
    fn = HANDLERS.get(ctx.kind)
    if fn is None:
        queue.finish(job_id, worker_id, "failed", f"Unknown job type: {ctx.kind}")
        return "failed"

    stop = threading.Event()

    def beat():
        interval = max(2.0, get_settings().job_lease_seconds / 3)
        while not stop.wait(interval):
            if not ctx.heartbeat():
                return

    hb = threading.Thread(target=beat, daemon=True)
    hb.start()
    try:
        ctx.progress("Starting", 0.0)
        result = fn(ctx) or {}
        queue.finish(job_id, worker_id, "succeeded", result=result)
        return "succeeded"
    except JobCancelled:
        queue.finish(job_id, worker_id, "cancelled")
        return "cancelled"
    except PermanentJobError as e:
        queue.finish(job_id, worker_id, "failed", str(e))
        return "failed"
    except Exception as e:  # noqa: BLE001 - worker must survive any handler error
        log.error("Job %s (%s) failed: %s", job_id, ctx.kind, e)
        log.debug(traceback.format_exc())
        queue.finish(job_id, worker_id, "retry", f"{type(e).__name__}: {e}")
        return "retry"
    finally:
        stop.set()


def _safe_run(job_id: str, worker_id: str) -> str:
    try:
        return run_one(job_id, worker_id)
    except Exception:  # noqa: BLE001 - even bookkeeping failures must not kill the pool thread silently
        log.exception("Job %s crashed outside its handler; it will be retried after its lease expires", job_id)
        return "crashed"


def drain(worker_id: str = "inline", max_jobs: int = 1000, kinds: list[str] | None = None) -> int:
    """Run queued jobs synchronously until none remain (used by tests and the sample command)."""
    from . import tasks  # noqa: F401  (registers handlers)

    n = 0
    while n < max_jobs:
        job_id = queue.claim_next(worker_id, kinds)
        if not job_id:
            break
        run_one(job_id, worker_id)
        n += 1
    return n


def main() -> None:
    from . import tasks  # noqa: F401

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    running = threading.Event()
    running.set()

    def _stop(*_):
        log.info("Worker stopping after current jobs…")
        running.clear()

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass

    log.info("Worker %s started (concurrency=%s)", worker_id, s.worker_concurrency)
    active: set = set()
    last_purge = 0.0
    with ThreadPoolExecutor(max_workers=s.worker_concurrency) as pool:
        while running.is_set():
            if s.retention_days and time.time() - last_purge > 3600:
                last_purge = time.time()
                try:
                    from ..services.retention import purge_expired

                    with SessionLocal() as db:
                        n = purge_expired(db)
                    if n:
                        log.info("Retention: deleted %s archived event(s)", n)
                except Exception as e:  # noqa: BLE001
                    log.warning("Retention purge failed: %s", e)
            active = {f for f in active if not f.done()}
            if len(active) >= s.worker_concurrency:
                time.sleep(0.2)
                continue
            try:
                job_id = queue.claim_next(worker_id)
            except Exception as e:  # noqa: BLE001 - e.g. "database is locked"; the worker must keep running
                log.warning("Could not claim a job (%s); retrying shortly", e.__class__.__name__)
                time.sleep(max(1.0, s.worker_poll_seconds))
                continue
            if not job_id:
                time.sleep(s.worker_poll_seconds)
                continue
            active.add(pool.submit(_safe_run, job_id, worker_id))
    log.info("Worker stopped")
