"""SerpAPI-based ranking collector for production use.

Uses the licensed SerpAPI service (https://serpapi.com) instead of direct browser
automation, complying with Google's terms of service for automated rank checking.
Evidence is saved as JSON API responses with SHA-256 hashes, same format as the
browser collector but without screenshots.
"""
import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .config import data_dir, s3_bucket, s3_prefix, serpapi_key
from .evidence import file_record, finalize, save_json, upload_to_s3
from .models import CheckRequest, matches_domain

log = logging.getLogger(__name__)

_RESULTS_PER_PAGE = 10


async def _render_page_screenshot(
    directory: Path,
    page_number: int,
    keyword: str,
    location: str,
    organic: list[dict],
    request: CheckRequest,
    google_url: str = "",
    browser=None,  # shared Playwright browser; if None a temporary one is created
) -> Path | None:
    """Render a clean evidence screenshot from SerpAPI organic results.

    We no longer attempt to capture the live Google page — Google immediately
    serves a CAPTCHA to headless browsers, which would end up as the "proof"
    screenshot. Instead we render a professional synthetic HTML page from the
    verified SerpAPI data and screenshot that.

    If *browser* is supplied it is reused (preferred); otherwise a short-lived
    browser is launched just for this page and closed when done.
    """
    png_path = directory / f"page-{page_number:02d}.png"

    def esc(v: object) -> str:
        return html.escape(str(v or ""))

    items_html = ""
    start_pos = (page_number - 1) * _RESULTS_PER_PAGE + 1
    for idx, item in enumerate(organic, start=start_pos):
        title = item.get("title", "")
        link = item.get("link", "")
        snippet = item.get("snippet", "")
        displayed_link = item.get("displayed_link", link)
        is_match = matches_domain(link, request.website, request.include_subdomains)
        highlight = "border: 2px solid #16a34a; background: #f0fdf4;" if is_match else ""
        badge = (
            '<span style="background:#16a34a;color:white;padding:3px 10px;'
            'border-radius:12px;font-size:12px;font-weight:bold;margin-left:8px;">'
            '✓ TARGET MATCH</span>'
            if is_match
            else ""
        )
        items_html += f"""
        <div style="margin-bottom:16px;padding:14px 18px;border-radius:12px;background:white;box-shadow:0 1px 4px rgba(0,0,0,0.10);{highlight}">
          <div style="font-size:12px;color:#5f6368;margin-bottom:3px;display:flex;align-items:center;gap:6px;">
            <span style="font-weight:700;color:#1a0dab;font-size:13px;">#{idx}</span>
            <span>{esc(displayed_link)}</span>{badge}
          </div>
          <div style="font-size:18px;color:#1a0dab;font-weight:500;margin-bottom:4px;line-height:1.3;">
            <a href="{esc(link)}" style="color:#1a0dab;text-decoration:none;">{esc(title)}</a>
          </div>
          <div style="font-size:14px;color:#4d5156;line-height:1.58;">{esc(snippet)}</div>
        </div>
        """

    loc_str = location or request.country
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_content = f"""<!doctype html><html><head><meta charset="utf-8">
    <style>
      body {{ font-family: system-ui, -apple-system, sans-serif; background: #f1f3f4; color: #202124; margin: 0; padding: 24px 30px; }}
      .container {{ max-width: 860px; margin: 0 auto; }}
      .header {{ background: white; padding: 18px 22px; border-radius: 14px; margin-bottom: 20px; box-shadow: 0 2px 6px rgba(0,0,0,0.08); }}
      .serp-label {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; color: #4285f4; text-transform: uppercase; margin-bottom: 6px; }}
      .search-box {{ font-size: 20px; font-weight: 600; color: #202124; margin-bottom: 10px; }}
      .meta {{ font-size: 13px; color: #5f6368; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }}
      .loc-badge {{ background:#e8eaff; color:#3730a3; padding:3px 10px; border-radius:14px; font-size:12px; font-weight:600; }}
      .evidence-badge {{ background:#dcfce7; color:#166534; padding:3px 10px; border-radius:14px; font-size:12px; font-weight:600; }}
      .footer {{ margin-top:18px; font-size:11px; color:#80868b; text-align:center; }}
    </style></head><body>
    <div class="container">
      <div class="header">
        <div class="serp-label">SerpAPI Verified Evidence · Page {page_number}</div>
        <div class="search-box">🔍 {esc(keyword)}</div>
        <div class="meta">
          <span>Target: <b>{esc(request.website)}</b></span>
          <span class="loc-badge">📍 {esc(loc_str)}</span>
          <span class="evidence-badge">✓ SerpAPI Verified</span>
          <span style="margin-left:auto;">{captured_at}</span>
        </div>
      </div>
      <div>{items_html}</div>
      <div class="footer">Evidence captured via SerpAPI licensed data feed — not direct browser scraping</div>
    </div></body></html>"""

    page_html_path = directory / f"page-{page_number:02d}.html"
    page_html_path.write_text(html_content, encoding="utf-8")

    own_browser = browser is None  # we launched it, so we must close it
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
        if own_browser:
            _pw_ctx = async_playwright()
            p = await _pw_ctx.__aenter__()
            try:
                browser = await p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = await p.chromium.launch(headless=True)
        pw_page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await pw_page.goto(page_html_path.resolve().as_uri(), wait_until="domcontentloaded")
        await pw_page.screenshot(path=str(png_path), full_page=True)
        await pw_page.close()
        if own_browser:
            await browser.close()
            await _pw_ctx.__aexit__(None, None, None)
        return png_path
    except Exception as exc:
        log.warning("Could not render synthetic screenshot: %s", exc)
        return None





def _safe_url(raw: str) -> str | None:
    """Return normalised https/http URL or None for invalid/Google-owned links."""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        if host == "google.com" or host.endswith(".google.com"):
            return None
        return raw
    except Exception:
        return None


def _location_matches(response: dict, requested: str) -> bool:
    """Return True when SerpAPI confirms the requested location was used."""
    if not requested:
        return False
    used: str = (
        response.get("search_parameters", {}).get("location", "")
        or response.get("search_information", {}).get("detected_location", "")
    )
    # Case-insensitive substring: "Chennai" matches "Chennai, Tamil Nadu, India"
    return requested.lower() in used.lower() or used.lower() in requested.lower()


async def collect(request: CheckRequest, *, output_root: Path | None = None) -> dict:
    """Run a SerpAPI-based ranking check and return a finalized observation dict."""
    run_id = uuid4().hex
    directory = (output_root or data_dir() / "evidence") / run_id
    directory.mkdir(parents=True, exist_ok=False)

    observation = {
        "run_id": run_id,
        "source": "serpapi",
        "request": request.model_dump(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "message": "",
        "organic_rank": None,
        "matched_url": None,
        "checked_depth": 0,
        "results": [],
        "evidence": [],
        "pages": [],
        "location_verified": False,
        "observed_location": None,
        "extractor_version": "serpapi-v1",
        "browser_version": None,
        "viewport": None,
    }

    try:
        import serpapi  # noqa: PLC0415
    except ImportError:
        observation.update(
            status="capture_failed",
            message="serpapi package is not installed. Run: pip install serpapi",
        )
        return finalize(directory, observation)

    try:
        key = serpapi_key()
    except RuntimeError as exc:
        observation.update(status="capture_failed", message=str(exc))
        return finalize(directory, observation)

    client = serpapi.Client(api_key=key)
    fingerprints: set[str] = set()

    # Open one shared Playwright browser for all synthetic screenshots.
    # This avoids launching Chrome once per page (expensive).
    _shared_browser = None
    _pw_ctx = None
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
        _pw_ctx = async_playwright()
        _p = await _pw_ctx.__aenter__()
        try:
            _shared_browser = await _p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            _shared_browser = await _p.chromium.launch(headless=True)
    except Exception as exc:
        log.warning("Could not launch shared screenshot browser: %s. Screenshots will be skipped.", exc)

    try:
        max_pages = (request.max_results + _RESULTS_PER_PAGE - 1) // _RESULTS_PER_PAGE
        for page_number in range(1, max_pages + 1):
            start = (page_number - 1) * _RESULTS_PER_PAGE
            params: dict = {
                "engine": "google",
                "q": request.keyword,
                "gl": request.country,
                "hl": request.language,
                "num": _RESULTS_PER_PAGE,
                "start": start,
                "safe": "off",
                "filter": "0",
            }
            if request.location:
                params["location"] = request.location

            try:
                response = client.search(params)
            except Exception as exc:
                if "location" in params:
                    loc_parts = [p.strip() for p in params["location"].split(",") if p.strip()]
                    if len(loc_parts) > 3:
                        params["location"] = ", ".join(loc_parts[1:])
                    elif len(loc_parts) > 1:
                        params["location"] = loc_parts[-1]
                    else:
                        params.pop("location", None)
                    try:
                        response = client.search(params)
                    except Exception as inner_exc:
                        exc = inner_exc
                if "response" not in locals():
                    exc_type = type(exc).__name__
                    if "SerpApi" in exc_type or "Http" in exc_type or "HTTP" in exc_type:
                        observation.update(
                            status="blocked",
                            message=f"SerpAPI returned an error ({exc_type}); check your location string or API key quota.",
                        )
                    else:
                        observation.update(
                            status="capture_failed",
                            message=f"Collection failed ({exc_type}); no verified rank published.",
                        )
                    break

            # Persist raw API response as evidence
            page_json = directory / f"page-{page_number:02d}.json"
            save_json(page_json, dict(response))
            observation["evidence"].append(file_record(page_json))
            observation["pages"].append({
                "page": page_number,
                "url": response.get("search_metadata", {}).get("google_url", ""),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            })

            # Verify location on first page
            if page_number == 1:
                location_match = _location_matches(response, request.location)
                observation["location_verified"] = location_match
                observation["observed_location"] = (
                    response.get("search_parameters", {}).get("location")
                    or response.get("search_information", {}).get("detected_location")
                )

            organic = response.get("organic_results", [])
            if organic:
                google_url = response.get("search_metadata", {}).get("google_url", "")
                png_path = await _render_page_screenshot(
                    directory, page_number, request.keyword, request.location,
                    organic, request, google_url=google_url,
                    browser=_shared_browser,
                )
                if png_path and png_path.exists():
                    observation["evidence"].append(file_record(png_path))

            if not organic:
                if page_number == 1:
                    error_info = response.get("error", "")
                    if error_info:
                        observation.update(status="blocked", message=str(error_info))
                    else:
                        observation.update(
                            status="extraction_failed",
                            message="SerpAPI returned no organic results on page 1.",
                        )
                else:
                    observation.update(
                        status="incomplete",
                        message=f"Results ended at page {page_number - 1}; absence not established.",
                    )
                break

            fingerprint = json.dumps(
                [r.get("link") for r in organic], sort_keys=True
            )
            if fingerprint in fingerprints:
                observation.update(
                    status="extraction_failed",
                    message="Pagination returned a duplicate results page.",
                )
                break
            fingerprints.add(fingerprint)

            remaining = request.max_results - len(observation["results"])
            for api_result in organic[:remaining]:
                url = _safe_url(api_result.get("link", ""))
                if not url:
                    continue
                observation["results"].append({
                    "title": api_result.get("title", ""),
                    "url": url,
                    "position": len(observation["results"]) + 1,
                    "page": page_number,
                })

            observation["checked_depth"] = len(observation["results"])

            match = next(
                (r for r in observation["results"]
                 if matches_domain(r["url"], request.website, request.include_subdomains)),
                None,
            )
            if match:
                location_note = (
                    "Location independently verified by SerpAPI."
                    if observation["location_verified"]
                    else "Requested location is not independently verified."
                )
                observation.update(
                    status="found",
                    organic_rank=match["position"],
                    matched_url=match["url"],
                    message=f"Observed via SerpAPI. {location_note}",
                )
                break

            if len(observation["results"]) >= request.max_results:
                observation.update(
                    status="not_found_within_depth",
                    message=(
                        f"Target absent from the first {request.max_results} "
                        "observed organic results (SerpAPI)."
                    ),
                )
                break
        else:
            if observation["status"] == "pending":
                observation.update(
                    status="incomplete",
                    message="Page limit reached before requested depth.",
                )

        if observation["status"] == "pending":
            observation.update(
                status="incomplete",
                message="Check ended without a conclusive result.",
            )

    except Exception as exc:
        observation.update(
            status="capture_failed",
            organic_rank=None,
            matched_url=None,
            message=f"Collection failed ({type(exc).__name__}); no verified rank published.",
        )
    finally:
        # Close the shared browser opened before the pagination loop.
        if _shared_browser is not None:
            try:
                await _shared_browser.close()
            except Exception:
                pass
        if _pw_ctx is not None:
            try:
                await _pw_ctx.__aexit__(None, None, None)
            except Exception:
                pass

    result = finalize(directory, observation)

    # Optional S3 upload — never blocks or raises on failure
    bucket = s3_bucket()
    if bucket:
        try:
            upload_to_s3(directory, bucket, s3_prefix())
        except Exception as exc:
            log.warning("S3 upload failed for run %s: %s", run_id, exc)

    return result
