"""Daily scheduler — enqueues one idempotent ranking job per configured keyword per day.

Uses APScheduler 3.x with a SQLAlchemyJobStore backed by the same database as the
job queue, so scheduler state survives restarts. The worker must be running separately
to actually execute the queued jobs.

Run with: rank-agent scheduler --config project.json
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import database_url, scheduler_hour, scheduler_minute
from .models import CheckRequest
from .store import Store

log = logging.getLogger(__name__)


def _keyword_slug(keyword: str) -> str:
    """Convert keyword to a safe slug for use in idempotency keys."""
    return re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")[:60]


def build_daily_key(request: CheckRequest, date_str: str | None = None) -> str:
    """Build a daily idempotency key: <website>-<keyword-slug>-<YYYY-MM-DD>."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{request.website}-{_keyword_slug(request.keyword)}-{date_str}"


def enqueue_daily(config_path: Path) -> None:
    """Read config and enqueue one job for today. Called by the scheduler trigger."""
    try:
        request = CheckRequest.model_validate_json(
            config_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        log.error("Scheduler: failed to read config %s: %s", config_path, exc)
        return

    key = build_daily_key(request)
    try:
        result = Store().enqueue(request, key)
        log.info(
            "Scheduler: enqueued job %s (key=%s, website=%s, keyword=%s)",
            result["id"], key, request.website, request.keyword,
        )
    except ValueError as exc:
        # Key already exists for a different request — misconfiguration
        log.error("Scheduler: idempotency key conflict for %s: %s", key, exc)
    except Exception as exc:
        log.error("Scheduler: enqueue failed for %s: %s", key, exc)


def run(config_path: Path) -> None:
    """Start the blocking daily scheduler. Runs until Ctrl+C."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        raise RuntimeError(
            "APScheduler is not installed. Run: pip install apscheduler"
        )

    hour = scheduler_hour()
    minute = scheduler_minute()

    jobstores = {
        "default": SQLAlchemyJobStore(url=database_url(), tablename="apscheduler_jobs")
    }
    scheduler = BlockingScheduler(jobstores=jobstores, timezone="UTC")

    scheduler.add_job(
        enqueue_daily,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[config_path],
        id="daily_ranking_check",
        name="Daily ranking check enqueue",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1 hour late fire after restart
    )

    log.info(
        "Scheduler started: daily enqueue at %s:%s UTC. Press Ctrl+C to stop.",
        hour.zfill(2), minute.zfill(2),
    )

    # Also enqueue immediately on startup (in case today's job was missed)
    try:
        enqueue_daily(config_path)
    except Exception as exc:
        log.warning("Startup enqueue skipped: %s", exc)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
