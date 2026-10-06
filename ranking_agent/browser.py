import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs, urljoin
from uuid import uuid4

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from .config import data_dir, headless
from .evidence import file_record, finalize, save_json
from .models import CheckRequest, matches_domain

EXTRACTOR = Path(__file__).with_name("extract.js").read_text(encoding="utf-8")
FIXTURE = Path(__file__).parent / "fixtures" / "results.html"


def valid_search_url(url: str, keyword: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "www.google.com"
            and parsed.port in (None, 443) and not parsed.username
            and parsed.path == "/search" and parse_qs(parsed.query).get("q") == [keyword])


async def collect(request: CheckRequest, *, demo: bool = False, output_root: Path | None = None) -> dict:
    run_id = uuid4().hex
    directory = (output_root or data_dir() / "evidence") / run_id
    directory.mkdir(parents=True, exist_ok=False)
    observation = {
        "run_id": run_id, "source": "fixture" if demo else "google_chrome",
        "request": request.model_dump(), "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending", "message": "", "organic_rank": None, "matched_url": None,
        "checked_depth": 0, "results": [], "evidence": [], "pages": [],
        "location_verified": False, "observed_location": None,
        "extractor_version": "conservative-dom-v1", "browser_version": None,
        "viewport": {"width": 1440, "height": 1000},
    }
    browser = None
    async with async_playwright() as p:
        try:
            try:
                browser = await p.chromium.launch(channel="chrome", headless=headless(), args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            except Exception:
                browser = await p.chromium.launch(headless=headless(), args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            observation["browser_version"] = browser.version
            context = await browser.new_context(
                viewport=observation["viewport"],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale=f"{request.language}-{request.country.upper()}"
            )
            page = await context.new_page()
            page.set_default_timeout(15000)
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # Only Google navigation is needed. Never visit result URLs or user-provided websites.
            async def guard(route):
                if route.request.is_navigation_request() and route.request.frame == page.main_frame:
                    host = urlsplit(route.request.url).hostname or ""
                    allowed = host == "google.com" or host.endswith(".google.com")
                    if not demo and not allowed:
                        await route.abort()
                        return
                await route.continue_()
            await context.route("**/*", guard)
            if demo:
                await page.goto(FIXTURE.as_uri())
            else:
                query_params = {"q": request.keyword, "hl": request.language, "gl": request.country, "pws": "0"}
                if request.location:
                    query_params["near"] = request.location
                query = urlencode(query_params)
                await page.goto(f"https://www.google.com/search?{query}", wait_until="domcontentloaded")
                # Handle Google consent button if present
                try:
                    consent_btn = page.locator("#L2AGLb, button[aria-label='Accept all']")
                    if await consent_btn.count() > 0:
                        await consent_btn.first.click()
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass
            visited = set()
            fingerprints = set()
            for page_number in range(1, 21):
                await page.wait_for_timeout(1000 if not demo else 100)
                body = (await page.locator("body").inner_text()).lower()
                status = None
                if "/sorry/" in page.url or "unusual traffic" in body or await page.locator('iframe[src*="recaptcha"], #captcha-form').count():
                    status = "blocked"
                elif "consent.google" in page.url or "before you continue to google" in body:
                    status = "consent_required"
                elif not demo and not valid_search_url(page.url, request.keyword):
                    status = "unexpected_page"

                # Wait for result structure, then require two matching reads around the screenshot.
                if not status:
                    try:
                        await page.locator("#rso h3").first.wait_for(timeout=10000)
                    except PlaywrightTimeout:
                        status = "extraction_failed"
                extracted = await page.evaluate(EXTRACTOR) if not status else {"results": [], "issues": [status]}
                screenshot = directory / f"page-{page_number:02d}.png"
                await page.screenshot(path=str(screenshot), full_page=True, animations="disabled")
                observation["evidence"].append(file_record(screenshot))
                save_json(directory / f"page-{page_number:02d}.json", {"url": page.url, **extracted})
                observation["pages"].append({"page": page_number, "url": page.url,
                                              "captured_at": datetime.now(timezone.utc).isoformat()})
                if status:
                    observation.update(status=status, message="Check stopped; inspect the saved screenshot.")
                    break
                after = await page.evaluate(EXTRACTOR)
                if extracted != after or extracted["issues"]:
                    observation.update(status="extraction_failed", message="Uncertain or changing page layout: " + "; ".join(extracted["issues"]))
                    break
                fingerprint = json.dumps(extracted["results"], sort_keys=True)
                if fingerprint in fingerprints:
                    observation.update(status="extraction_failed", message="Pagination repeated a results page.")
                    break
                fingerprints.add(fingerprint)
                remaining = request.max_results - len(observation["results"])
                for result in extracted["results"][:remaining]:
                    observation["results"].append({**result, "position": len(observation["results"]) + 1,
                                                    "page": page_number})
                observation["checked_depth"] = len(observation["results"])
                match = next((r for r in observation["results"] if matches_domain(r["url"], request.website, request.include_subdomains)), None)
                if match:
                    observation.update(status="found", organic_rank=match["position"], matched_url=match["url"],
                                       message="Observed in this Chrome session; requested location is not independently verified.")
                    break
                if len(observation["results"]) >= request.max_results:
                    observation.update(status="not_found_within_depth", message=f"Target absent from the first {request.max_results} observed organic results.")
                    break
                next_link = page.locator("a#pnnext")
                if demo or await next_link.count() != 1:
                    observation.update(status="incomplete", message="Pagination ended before the requested depth; no absence claim made.")
                    break
                next_url = await next_link.get_attribute("href")
                next_url = urljoin(page.url, next_url or "")
                if not valid_search_url(next_url, request.keyword) or next_url in visited:
                    observation.update(status="extraction_failed", message="Unexpected pagination link.")
                    break
                visited.add(next_url)
                await page.goto(next_url, wait_until="domcontentloaded")
            else:
                observation.update(status="incomplete", message="Page limit reached before requested depth.")
        except PlaywrightTimeout:
            observation.update(status="timeout", organic_rank=None, matched_url=None, message="Chrome timed out. Retry later.")
        except Exception as exc:
            # Do not expose raw browser errors, URLs, environment values, or credentials.
            observation.update(status="capture_failed" if browser else "browser_failed", organic_rank=None,
                               matched_url=None, message=f"Collection failed ({type(exc).__name__}); no verified rank published.")
        finally:
            if browser:
                await browser.close()
    return finalize(directory, observation)
