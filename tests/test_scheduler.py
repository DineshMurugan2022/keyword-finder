"""Tests for the daily scheduler: key format and enqueue behaviour."""
from datetime import date

import pytest

from ranking_agent.models import CheckRequest
from ranking_agent.scheduler import build_daily_key, enqueue_daily
from ranking_agent.store import Store


def test_daily_key_format():
    request = CheckRequest(
        website="jkmtraders.in",
        keyword="building demolition in Chennai",
        location="Chennai, India",
    )
    key = build_daily_key(request, date_str="2026-09-10")
    assert key == "jkmtraders.in-building-demolition-in-chennai-2026-09-10"


def test_daily_key_is_idempotent_for_same_day():
    request = CheckRequest(website="example.com", keyword="demo keyword")
    today = date.today().isoformat()
    assert build_daily_key(request, date_str=today) == build_daily_key(request, date_str=today)


def test_scheduler_enqueues_job(tmp_path):
    """enqueue_daily should insert a job into the store when config is valid."""
    config = tmp_path / "project.json"
    config.write_text(
        '{"website": "example.com", "keyword": "test keyword", '
        '"country": "in", "language": "en", "location": "Chennai, India", '
        '"max_results": 10, "include_subdomains": true}',
        encoding="utf-8",
    )
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    store = Store(db_url)

    # Patch the Store class to use the test database
    import ranking_agent.scheduler as sched_module
    original_store = sched_module.Store

    class PatchedStore(Store):
        def __init__(self, url=None):
            super().__init__(db_url)

    sched_module.Store = PatchedStore
    try:
        enqueue_daily(config)
    finally:
        sched_module.Store = original_store

    # Verify that a job was queued
    with store.engine.connect() as conn:
        from ranking_agent.store import jobs
        from sqlalchemy import select
        rows = conn.execute(select(jobs)).mappings().all()

    assert len(rows) == 1
    assert rows[0]["status"] == "queued"
    assert "example.com" in rows[0]["idempotency_key"]
    assert "test-keyword" in rows[0]["idempotency_key"]


def test_scheduler_idempotent_second_call(tmp_path):
    """Calling enqueue_daily twice for the same day should not create a second job."""
    config = tmp_path / "project.json"
    config.write_text(
        '{"website": "example.com", "keyword": "test keyword", '
        '"country": "in", "language": "en", "location": "", '
        '"max_results": 10, "include_subdomains": true}',
        encoding="utf-8",
    )
    db_url = f"sqlite:///{tmp_path / 'test2.db'}"
    store = Store(db_url)

    import ranking_agent.scheduler as sched_module
    original_store = sched_module.Store

    class PatchedStore(Store):
        def __init__(self, url=None):
            super().__init__(db_url)

    sched_module.Store = PatchedStore
    try:
        enqueue_daily(config)
        enqueue_daily(config)  # second call — same day, same key
    finally:
        sched_module.Store = original_store

    with store.engine.connect() as conn:
        from ranking_agent.store import jobs
        from sqlalchemy import select
        rows = conn.execute(select(jobs)).mappings().all()

    assert len(rows) == 1, "Second call should reuse existing job, not create a new one"
