"""Durable queue with compare-and-swap leases; SQLite locally, PostgreSQL in deployment."""
import json
import logging
import time
from uuid import uuid4

from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text, create_engine, select, update
from sqlalchemy.exc import IntegrityError

from .config import LEASE_DURATION_SECONDS, database_url
from .models import CheckRequest

log = logging.getLogger(__name__)

metadata = MetaData()
jobs = Table("ranking_jobs", metadata,
    Column("id", String(32), primary_key=True), Column("idempotency_key", String(200), unique=True),
    Column("request", Text, nullable=False), Column("status", String(20), nullable=False),
    Column("created_at", Float, nullable=False), Column("available_at", Float, nullable=False),
    Column("lease_until", Float, nullable=False, default=0), Column("lease_token", String(32)),
    Column("attempts", Integer, nullable=False, default=0), Column("result", Text),
    Column("priority", Integer, nullable=False, server_default="0", default=0))


class Store:
    def __init__(self, url: str | None = None):
        url = url or database_url()
        self.engine = create_engine(url, connect_args={"timeout": 30} if url.startswith("sqlite") else {}, pool_pre_ping=True)
        metadata.create_all(self.engine)
        self._ensure_priority_column()

    def _ensure_priority_column(self) -> None:
        """One-time migration shim: add the priority column if it was missing.

        This is retained for backward compatibility with existing SQLite databases
        that were created before the priority column was added.  For a clean
        deployment use Alembic or simply drop and recreate the database.
        """
        from sqlalchemy import text
        try:
            with self.engine.begin() as conn:
                conn.execute(select(jobs.c.priority).limit(1))
        except Exception:
            try:
                with self.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE ranking_jobs ADD COLUMN priority INTEGER DEFAULT 0"))
                log.info("Migrated ranking_jobs: added priority column.")
            except Exception as exc:
                log.warning("Could not add priority column (may already exist): %s", exc)

    def enqueue(self, request: CheckRequest, key: str, priority: int = 0) -> dict:
        if not 1 <= len(key) <= 200:
            raise ValueError("Idempotency key must contain 1–200 characters")
        payload = request.model_dump_json()
        job_id = uuid4().hex
        try:
            with self.engine.begin() as conn:
                conn.execute(jobs.insert().values(id=job_id, idempotency_key=key, request=payload,
                    status="queued", created_at=time.time(), available_at=time.time(), lease_until=0, attempts=0, priority=priority))
        except IntegrityError:
            with self.engine.connect() as conn:
                row = conn.execute(select(jobs).where(jobs.c.idempotency_key == key)).mappings().one()
            if row["request"] != payload:
                raise ValueError("Idempotency key already belongs to a different request")
            job_id = row["id"]
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
        if not row:
            return None
        value = dict(row)
        value["request"] = json.loads(value["request"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        value.pop("lease_token", None)
        return value

    def claim(self) -> dict | None:
        now = time.time()
        with self.engine.begin() as conn:
            # Expired third attempts become explicit failures instead of getting stuck.
            conn.execute(update(jobs).where(jobs.c.status == "running", jobs.c.lease_until < now,
                         jobs.c.attempts >= 3).values(status="failed", result=json.dumps({"status": "worker_lost"})))
            eligible = ((jobs.c.status == "queued") & (jobs.c.available_at <= now)) | ((jobs.c.status == "running") & (jobs.c.lease_until < now))
            row = conn.execute(select(jobs).where(eligible, jobs.c.attempts < 3).order_by(jobs.c.priority.desc(), jobs.c.created_at.asc()).limit(1)).mappings().first()
            if not row:
                return None
            token = uuid4().hex
            changed = conn.execute(update(jobs).where(jobs.c.id == row["id"], eligible,
                jobs.c.attempts == row["attempts"]).values(status="running", lease_token=token,
                lease_until=now + LEASE_DURATION_SECONDS, attempts=row["attempts"] + 1))
            if changed.rowcount != 1:
                return None
            return {"id": row["id"], "request": json.loads(row["request"]),
                    "lease_token": token, "attempts": row["attempts"] + 1}

    def finish(self, job: dict, result: dict) -> None:
        # 'worker_failed' is transient (process crash / OOM) and should be retried.
        # 'capture_failed' is a permanent application-level error; do not retry.
        transient = result["status"] in {"timeout", "browser_failed", "worker_failed"}
        retry = transient and job["attempts"] < 3
        success = result["status"] in {"found", "not_found_within_depth", "incomplete"}
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"], jobs.c.lease_token == job["lease_token"],
                jobs.c.status == "running").values(status="queued" if retry else ("completed" if success else "failed"),
                available_at=time.time() + 30 * job["attempts"], lease_until=0,
                result=json.dumps(result), lease_token=None))

    def release_orphaned_leases(self) -> int:
        """Reset any 'running' jobs to 'queued' so they can be re-claimed by a fresh worker.

        Call this once at worker startup to recover from a previous crash/kill.
        Jobs that have already exhausted all 3 attempts are marked failed instead.
        """
        now = time.time()
        with self.engine.begin() as conn:
            # Permanently fail exhausted jobs
            conn.execute(
                update(jobs)
                .where(jobs.c.status == "running", jobs.c.attempts >= 3)
                .values(status="failed", lease_until=0, lease_token=None,
                        result=json.dumps({"status": "worker_lost",
                                          "message": "Worker process died; no more retries.",
                                          "organic_rank": None}))
            )
            # Re-queue everything else that is still "running" (orphaned leases)
            result = conn.execute(
                update(jobs)
                .where(jobs.c.status == "running")
                .values(status="queued", lease_until=0, lease_token=None,
                        available_at=now)
            )
            return result.rowcount

    def list_recent(self, limit: int = 50) -> list[dict]:
        """Return the most recent jobs (newest first) for the history panel."""
        from sqlalchemy import desc
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(jobs).order_by(desc(jobs.c.created_at)).limit(limit)
            ).mappings().all()
        out = []
        for row in rows:
            v = dict(row)
            v["request"] = json.loads(v["request"])
            v["result"] = json.loads(v["result"]) if v["result"] else None
            v.pop("lease_token", None)
            out.append(v)
        return out
