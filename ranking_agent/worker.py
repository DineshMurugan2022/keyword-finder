import asyncio
import logging

from .config import WORKER_TIMEOUT_SECONDS
from .models import CheckRequest
from .serpapi_collector import collect
from .store import Store

log = logging.getLogger(__name__)


async def work_once(store: Store) -> bool:
    job = store.claim()
    if not job:
        return False
    try:
        # Total deadline is shorter than the DB lease (LEASE_DURATION_SECONDS)
        # to prevent a second worker from stealing the slot before we finish.
        result = await asyncio.wait_for(
            collect(CheckRequest.model_validate(job["request"])),
            timeout=WORKER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        result = {"status": "timeout", "message": "Worker deadline exceeded", "organic_rank": None}
    except Exception as exc:
        log.exception("Unexpected worker error for job %s: %s", job["id"], exc)
        result = {"status": "worker_failed", "message": "Worker could not persist collection", "organic_rank": None}
    store.finish(job, result)
    log.info("job=%s status=%s", job["id"], result["status"])
    return True


async def run(once: bool = False, workers: int = 3) -> None:
    store = Store()
    if once:
        await work_once(store)
        return

    # On a fresh start, immediately re-queue any jobs that were left "running"
    # by a previous worker process that was killed/crashed.
    released = store.release_orphaned_leases()
    if released:
        log.info("Released %d orphaned job lease(s) from previous session", released)

    async def worker_loop(worker_id: int):
        while True:
            processed = await work_once(store)
            if not processed:
                await asyncio.sleep(1)

    log.info("Starting worker pool with %d concurrent tasks", workers)
    tasks = [asyncio.create_task(worker_loop(i)) for i in range(workers)]
    await asyncio.gather(*tasks)
