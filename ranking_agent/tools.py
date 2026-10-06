from datetime import datetime, timezone

from .models import CheckRequest
from .store import Store


def submit_ranking_check(website: str, keyword: str, country: str, language: str,
                         location: str, max_results: int, request_id: str) -> dict:
    """Queue one desktop Chrome organic ranking check. request_id is a unique idempotency key.

    Country and language are two-letter codes. Location is a requested label, not a
    verified city setting. Results arrive when the separately running worker finishes.
    Reuse request_id only when retrying the exact same submission.
    """
    try:
        request = CheckRequest(website=website, keyword=keyword, country=country,
                               language=language, location=location, max_results=max_results)
        return Store().enqueue(request, request_id)
    except ValueError as exc:
        return {"status": "invalid_request", "message": str(exc)}


def get_ranking_check(job_id: str) -> dict:
    """Read a previously queued check. Only its stored result can support ranking claims."""
    return Store().get(job_id) or {"status": "not_found", "message": "Unknown job ID"}


def submit_bulk_ranking_checks(website: str, keywords: list[str], locations: list[str] | None = None,
                               country: str = "in", language: str = "en",
                               max_results: int = 30) -> dict:
    """Queue bulk desktop Chrome organic ranking checks for one target website across multiple keywords and locations.

    Submits each keyword and location combination as a durable check job.
    """
    store = Store()
    submitted = []
    errors = []
    loc_list = [loc.strip() for loc in locations if loc.strip()] if locations else ["Chennai, Tamil Nadu, India"]

    for kw in keywords:
        kw_clean = kw.strip()
        if not kw_clean:
            continue
        for loc_clean in loc_list:
            # Include today's UTC date so the same keywords submitted tomorrow
            # get a fresh check instead of returning the cached result.
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            request_id = f"{website}-{kw_clean}-{loc_clean}-{today}".replace(" ", "-")[:190]
            try:
                request = CheckRequest(website=website, keyword=kw_clean, country=country,
                                       language=language, location=loc_clean, max_results=max_results)
                job = store.enqueue(request, request_id)
                submitted.append({
                    "job_id": job["id"],
                    "keyword": kw_clean,
                    "location": loc_clean,
                    "status": job["status"],
                })
            except Exception as exc:
                errors.append({"keyword": kw_clean, "location": loc_clean, "error": str(exc)})

    return {
        "website": website,
        "total_queued": len(submitted),
        "jobs": submitted,
        "errors": errors,
    }


def get_bulk_ranking_checks(job_ids: list[str]) -> dict:
    """Read stored results for a list of queued job IDs and return a consolidated report."""
    store = Store()
    results = []
    for job_id in job_ids:
        job = store.get(job_id)
        if job:
            results.append(job)
        else:
            results.append({"id": job_id, "status": "not_found", "message": "Unknown job ID"})
    return {"total": len(results), "jobs": results}


def validate_ranking_check(job_id: str) -> dict:
    """Validate a completed check job ID against evidence integrity, PNG screenshots, domain matching, and rank claims."""
    from .validator import validate_job
    return validate_job(job_id)


