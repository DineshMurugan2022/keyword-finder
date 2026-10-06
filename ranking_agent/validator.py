"""Validation module for ranking check jobs and evidence integrity."""
import json
import logging
from pathlib import Path

from .config import data_dir
from .evidence import verify as verify_evidence
from .store import Store

log = logging.getLogger(__name__)


def validate_check_directory(directory: Path) -> dict:
    """Validate evidence directory integrity, screenshots, rank claim, and domain match."""
    directory = Path(directory)
    if not directory.is_dir():
        return {
            "valid": False,
            "file_integrity_valid": False,
            "screenshots_valid": False,
            "rank_claim_valid": False,
            "domain_match_valid": False,
            "error": f"Directory '{directory}' does not exist",
        }

    # 1. File integrity check via manifest hashes
    try:
        file_integrity_valid = verify_evidence(directory)
    except Exception as exc:
        log.warning("File integrity check failed for %s: %s", directory, exc)
        file_integrity_valid = False

    # Load observation.json
    obs_path = directory / "observation.json"
    if not obs_path.is_file():
        return {
            "valid": False,
            "file_integrity_valid": file_integrity_valid,
            "screenshots_valid": False,
            "rank_claim_valid": False,
            "domain_match_valid": False,
            "error": "observation.json missing",
        }

    try:
        observation = json.loads(obs_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "valid": False,
            "file_integrity_valid": file_integrity_valid,
            "screenshots_valid": False,
            "rank_claim_valid": False,
            "domain_match_valid": False,
            "error": f"Invalid observation.json format: {exc}",
        }

    # 2. Screenshots valid
    screenshots_valid = True
    evidence_items = observation.get("evidence", [])
    png_files = [item["file"] for item in evidence_items if isinstance(item, dict) and item.get("file", "").endswith(".png")]
    for png_name in png_files:
        png_path = directory / png_name
        if not (png_path.is_file() and png_path.stat().st_size > 0):
            screenshots_valid = False
            break

    # 3. Rank claim valid
    status = observation.get("status")
    organic_rank = observation.get("organic_rank")
    if status == "found":
        rank_claim_valid = isinstance(organic_rank, int) and organic_rank >= 1
    elif status in {"not_found_within_depth", "incomplete", "failed"}:
        rank_claim_valid = organic_rank is None
    else:
        rank_claim_valid = True

    # 4. Domain match valid
    website = (observation.get("request") or {}).get("website", "")
    domain_match_valid = True
    if status == "found" and website and organic_rank:
        results = observation.get("results", [])
        matched = False
        for item in results:
            if item.get("position") == organic_rank:
                url = item.get("url", "")
                if website.lower() in url.lower():
                    matched = True
                    break
        domain_match_valid = matched

    all_valid = (
        file_integrity_valid
        and screenshots_valid
        and rank_claim_valid
        and domain_match_valid
    )

    return {
        "valid": all_valid,
        "file_integrity_valid": file_integrity_valid,
        "screenshots_valid": screenshots_valid,
        "rank_claim_valid": rank_claim_valid,
        "domain_match_valid": domain_match_valid,
        "directory": str(directory),
        "status": status,
        "organic_rank": organic_rank,
    }


def validate_job(job_id: str, store: Store | None = None) -> dict:
    """Validate a completed check job ID against database, evidence directory, and rank claims."""
    if store is None:
        store = Store()

    job = store.get(job_id)
    if not job:
        return {
            "job_id": job_id,
            "valid": False,
            "error": f"Job '{job_id}' not found in database",
        }

    status = job.get("status")
    result = job.get("result") or {}

    if status != "completed":
        return {
            "job_id": job_id,
            "valid": False,
            "status": status,
            "error": f"Job is in '{status}' status (must be completed to validate)",
        }

    run_id = result.get("run_id")
    if not run_id:
        return {
            "job_id": job_id,
            "valid": False,
            "status": status,
            "error": "Job result is missing run_id",
        }

    evidence_path = data_dir() / run_id
    report = validate_check_directory(evidence_path)
    report["job_id"] = job_id
    report["run_id"] = run_id
    return report
