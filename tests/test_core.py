import json
import time

import pytest
from sqlalchemy import update

from ranking_agent.browser import collect, valid_search_url
from ranking_agent.evidence import verify
from ranking_agent.models import CheckRequest, matches_domain
from ranking_agent.store import Store, jobs


def test_domain_matching():
    assert matches_domain("https://www.jkmtraders.in/services", "jkmtraders.in")
    assert not matches_domain("https://jkmtraders.in.evil.test", "jkmtraders.in")
    assert not matches_domain("https://eviljkmtraders.in", "jkmtraders.in")
    assert not matches_domain("https://www.jkmtraders.in", "jkmtraders.in", False)
    assert CheckRequest(website="https://JKMTRADERS.IN/path", keyword=" hi ").website == "jkmtraders.in"


def test_navigation_constraint():
    assert valid_search_url("https://www.google.com/search?q=test&start=10", "test")
    assert not valid_search_url("https://evil.test/search?q=test", "test")
    assert not valid_search_url("https://www.google.com/search?q=other", "test")


def test_input_validation():
    for update_values in ({"keyword": " "}, {"max_results": 101}, {"country": "India"}):
        with pytest.raises(ValueError):
            CheckRequest(**({"website": "example.com", "keyword": "demo"} | update_values))


def test_queue_idempotency_and_stale_lease(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'queue.db'}")
    request = CheckRequest(website="example.com", keyword="demo")
    first = store.enqueue(request, "once")
    assert store.enqueue(request, "once")["id"] == first["id"]
    with pytest.raises(ValueError):
        store.enqueue(CheckRequest(website="example.com", keyword="different"), "once")
    old = store.claim()
    assert store.claim() is None
    with store.engine.begin() as conn:
        conn.execute(update(jobs).values(lease_until=time.time() - 1))
    new = store.claim()
    store.finish(old, {"status": "found", "organic_rank": 99})
    assert store.get(first["id"])["status"] == "running"
    store.finish(new, {"status": "found", "organic_rank": 3})
    assert store.get(first["id"])["result"]["organic_rank"] == 3


@pytest.mark.asyncio
async def test_chrome_evidence_and_tamper_detection(tmp_path):
    result = await collect(CheckRequest(website="example.com", keyword="example keyword", max_results=3),
                           demo=True, output_root=tmp_path)
    assert result["status"] == "found", result
    assert result["organic_rank"] == 3
    assert result["source"] == "fixture"
    directory = tmp_path / result["run_id"]
    assert verify(directory)
    with (directory / "page-01.png").open("ab") as file:
        file.write(b"tampered")
    assert not verify(directory)


@pytest.mark.asyncio
async def test_absence_requires_requested_depth(tmp_path):
    shallow = await collect(CheckRequest(website="absent.test", keyword="example", max_results=3), demo=True, output_root=tmp_path)
    assert shallow["status"] == "not_found_within_depth"
    deep = await collect(CheckRequest(website="absent.test", keyword="example", max_results=30), demo=True, output_root=tmp_path)
    assert deep["status"] == "incomplete"
    assert deep["organic_rank"] is None


@pytest.mark.asyncio
async def test_unknown_layout_cannot_publish_rank(monkeypatch, tmp_path):
    fixture = tmp_path / "unknown.html"
    fixture.write_text('<div id="rso"><a href="https://example.com"><h3>Unknown card</h3></a></div>')
    monkeypatch.setattr("ranking_agent.browser.FIXTURE", fixture)
    result = await collect(CheckRequest(website="example.com", keyword="demo"), demo=True, output_root=tmp_path)
    assert result["status"] == "extraction_failed"
    assert result["organic_rank"] is None
    assert result["evidence"]


@pytest.mark.asyncio
async def test_missing_screenshot_cannot_publish_rank(monkeypatch, tmp_path):
    from playwright.async_api import Page
    async def broken_screenshot(*args, **kwargs):
        raise OSError("disk failure")
    monkeypatch.setattr(Page, "screenshot", broken_screenshot)
    result = await collect(CheckRequest(website="example.com", keyword="demo"), demo=True, output_root=tmp_path)
    assert result["status"] == "capture_failed"
    assert result["organic_rank"] is None


def test_adk_tool_schemas():
    from google.adk.tools import FunctionTool
    from ranking_agent.tools import submit_ranking_check, get_ranking_check
    for function in (submit_ranking_check, get_ranking_check):
        declaration = FunctionTool(function)._get_declaration()
        assert declaration.name == function.__name__


# ── SerpAPI collector tests ─────────────────────────────────────────────────

def _serpapi_page(results, location="Chennai, Tamil Nadu, India"):
    """Build a minimal SerpAPI response dict for testing."""
    return {
        "search_parameters": {"location": location},
        "search_metadata": {"google_url": "https://www.google.com/search?q=test"},
        "organic_results": [
            {"position": i + 1, "title": r["title"], "link": r["link"]}
            for i, r in enumerate(results)
        ],
    }


@pytest.mark.asyncio
async def test_serpapi_collector_found_with_location_verified(monkeypatch, tmp_path):
    import serpapi as serpapi_module
    page = _serpapi_page([
        {"title": "Result 1", "link": "https://other.com/page"},
        {"title": "Result 2", "link": "https://other2.com/page"},
        {"title": "JKM Traders", "link": "https://jkmtraders.in/services"},
    ])
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    monkeypatch.setattr(serpapi_module.Client, "search", lambda self, params: page)
    from ranking_agent.serpapi_collector import collect
    result = await collect(
        CheckRequest(website="jkmtraders.in", keyword="building demolition in Chennai",
                     location="Chennai, Tamil Nadu, India", max_results=10),
        output_root=tmp_path,
    )
    assert result["status"] == "found", result
    assert result["organic_rank"] == 3
    assert result["location_verified"] is True
    assert result["source"] == "serpapi"
    # Evidence JSON file must exist
    directory = tmp_path / result["run_id"]
    assert (directory / "page-01.json").is_file()
    assert verify(directory)


@pytest.mark.asyncio
async def test_serpapi_collector_not_found_within_depth(monkeypatch, tmp_path):
    import serpapi as serpapi_module
    results = [{"title": f"Other {i}", "link": f"https://other{i}.com/"} for i in range(10)]
    page = _serpapi_page(results)
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    monkeypatch.setattr(serpapi_module.Client, "search", lambda self, params: page)
    from ranking_agent.serpapi_collector import collect
    result = await collect(
        CheckRequest(website="absent.test", keyword="demo keyword", max_results=10),
        output_root=tmp_path,
    )
    assert result["status"] == "not_found_within_depth"
    assert result["organic_rank"] is None


@pytest.mark.asyncio
async def test_serpapi_collector_blocked_on_api_error(monkeypatch, tmp_path):
    import serpapi as serpapi_module
    monkeypatch.setenv("SERPAPI_KEY", "test-key")

    class FakeSerpApiError(Exception):
        pass

    def raise_error(self, params):
        raise FakeSerpApiError("quota exceeded")

    # Patch the exception name to trigger the 'blocked' branch
    FakeSerpApiError.__name__ = "SerpApiError"
    monkeypatch.setattr(serpapi_module.Client, "search", raise_error)
    from ranking_agent.serpapi_collector import collect
    result = await collect(
        CheckRequest(website="example.com", keyword="demo keyword", max_results=10),
        output_root=tmp_path,
    )
    assert result["status"] == "blocked"
    assert result["organic_rank"] is None


def test_queue_finish_incomplete_is_completed(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'queue.db'}")
    request = CheckRequest(website="example.com", keyword="demo")
    job = store.enqueue(request, "incomplete_test")
    claimed = store.claim()
    store.finish(claimed, {"status": "incomplete", "message": "Page limit reached", "organic_rank": None})
    fetched = store.get(job["id"])
    assert fetched["status"] == "completed"
    assert fetched["result"]["status"] == "incomplete"


def test_bulk_ranking_checks_submission_and_retrieval(tmp_path, monkeypatch):
    monkeypatch.setattr("ranking_agent.tools.Store", lambda: Store(f"sqlite:///{tmp_path / 'queue.db'}"))
    from ranking_agent.tools import get_bulk_ranking_checks, submit_bulk_ranking_checks

    keywords = ["building demolition", "scrap buyers"]
    locations = ["Chennai, Tamil Nadu, India", "Adambakkam, Chennai, Tamil Nadu, India"]

    batch = submit_bulk_ranking_checks("jkmtraders.in", keywords, locations)
    assert batch["total_queued"] == 4
    assert len(batch["jobs"]) == 4

    job_ids = [j["job_id"] for j in batch["jobs"]]
    report = get_bulk_ranking_checks(job_ids)
    assert report["total"] == 4


@pytest.mark.asyncio
async def test_validator_module(tmp_path):
    from ranking_agent.browser import collect
    from ranking_agent.validator import validate_check_directory

    request = CheckRequest(website="example.com", keyword="example keyword", max_results=3)
    res = await collect(request, demo=True, output_root=tmp_path)
    directory = tmp_path / res["run_id"]

    val_report = validate_check_directory(directory)
    assert val_report["valid"] is True
    assert val_report["file_integrity_valid"] is True
    assert val_report["screenshots_valid"] is True
    assert val_report["rank_claim_valid"] is True
    assert val_report["domain_match_valid"] is True



