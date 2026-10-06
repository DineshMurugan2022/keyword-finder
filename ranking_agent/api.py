import os
import re
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import data_dir
from .models import CheckRequest
from .store import Store
from .validator import validate_job

bearer = HTTPBearer(auto_error=False)

# ── Auth ───────────────────────────────────────────────────────────────────

def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    expected = os.getenv("RANK_API_TOKEN", "")
    if len(expected) < 32:
        raise HTTPException(503, "Configure RANK_API_TOKEN with at least 32 characters")
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(401, "Invalid credentials")


# ── Store singleton ────────────────────────────────────────────────────────
# A single Store (and therefore a single SQLAlchemy engine / connection pool)
# is reused across all requests instead of being recreated per-request.

_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Chrome ranking evidence", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=b"", media_type="image/x-icon")


@app.get("/")
@app.get("/dashboard")
def dashboard():
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Custom UI not initialized</h1>", status_code=404)
    token = os.getenv("RANK_API_TOKEN", "")
    html = html_path.read_text(encoding="utf-8")
    # Inject the server-side token into the page so JS can authenticate API calls.
    # The placeholder __RANK_API_TOKEN__ is replaced at serve-time, never stored in git.
    html = html.replace("__RANK_API_TOKEN__", token)
    return HTMLResponse(html)


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Public evidence (read-only, no secrets) ────────────────────────────────

@app.get("/public/evidence/{run_id}/{filename}")
def public_evidence(run_id: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or not re.fullmatch(
        r"(?:page-\d{2}\.(?:png|json)|observation\.json|manifest\.json|report\.html)", filename
    ):
        raise HTTPException(404, "Unknown evidence")
    path = data_dir(create=False) / "evidence" / run_id / filename
    if not path.is_file():
        raise HTTPException(404, "Unknown evidence")
    return FileResponse(path)


# ── Dashboard / public API (protected) ────────────────────────────────────

@app.post("/api/submit", dependencies=[Depends(authorize)])
def api_submit(request: CheckRequest, store: Store = Depends(get_store)):
    # Strip any leading/trailing commas or whitespace from location
    sanitized = request.model_copy(update={"location": request.location.strip(", ").strip()})
    req_id = f"web-{secrets.token_hex(4)}"
    return store.enqueue(sanitized, req_id, priority=10)


@app.get("/api/jobs", dependencies=[Depends(authorize)])
def api_list_jobs(limit: int = 50, store: Store = Depends(get_store)):
    return store.list_recent(limit=min(limit, 100))


@app.post("/api/submit-bulk", dependencies=[Depends(authorize)])
def api_submit_bulk(payload: dict, store: Store = Depends(get_store)):
    website = payload.get("website", "")
    keywords = payload.get("keywords", [])
    locations = payload.get("locations", ["Chennai, Tamil Nadu, India"])
    max_results = payload.get("max_results", 30)
    # Accept country/language from payload; fall back to sensible defaults.
    country = payload.get("country", "in")
    language = payload.get("language", "en")

    submitted = []
    for kw in keywords:
        for loc in locations:
            req = CheckRequest(
                website=website,
                keyword=kw,
                location=loc,
                max_results=max_results,
                country=country,
                language=language,
            )
            req_id = f"bulk-{secrets.token_hex(6)}"
            job = store.enqueue(req, req_id, priority=0)
            submitted.append(job)
    return {"status": "enqueued", "jobs": submitted}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(authorize)])
def api_job_status(job_id: str, store: Store = Depends(get_store)):
    result = store.get(job_id)
    if not result:
        raise HTTPException(404, "Unknown job")
    return result


@app.get("/api/validate/{job_id}", dependencies=[Depends(authorize)])
def api_validate_job(job_id: str):
    return validate_job(job_id)


# ── Authenticated REST API (external callers) ──────────────────────────────

@app.post("/jobs", dependencies=[Depends(authorize)], status_code=202)
def submit(
    request: CheckRequest,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    store: Store = Depends(get_store),
):
    try:
        return store.enqueue(request, idempotency_key, priority=10)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/jobs/{job_id}", dependencies=[Depends(authorize)])
def read(job_id: str, store: Store = Depends(get_store)):
    result = store.get(job_id)
    if not result:
        raise HTTPException(404, "Unknown job")
    return result


@app.get("/evidence/{run_id}/{filename}", dependencies=[Depends(authorize)])
def evidence(run_id: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or not re.fullmatch(
        r"(?:page-\d{2}\.(?:png|json)|observation\.json|manifest\.json|report\.html)", filename
    ):
        raise HTTPException(404, "Unknown evidence")
    path = data_dir(create=False) / "evidence" / run_id / filename
    if not path.is_file():
        raise HTTPException(404, "Unknown evidence")
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        },
    )


