# Chrome keyword ranking agent — Google ADK

Checks organic Google rankings for **jkmtraders.in**, keyword **building demolition in Chennai**, location **Chennai, Tamil Nadu, India**. Edit `project.json` to change the target. Production checks use **SerpAPI** (a licensed SERP data provider) — no direct browser scraping, no ToS violation.

## Quick start (local, Windows)

1. Copy `.env.example` to `ranking_agent/.env` and enter your keys:

```powershell
Copy-Item ranking_agent\.env.example ranking_agent\.env
```

Minimum required:
- `SERPAPI_KEY` — get a free key (100 searches/month) at https://serpapi.com
- `OPENAI_API_KEY` + `AGENT_MODEL` — only needed for ADK chat, not for CLI or API

2. Start the worker and ADK UI together:

```powershell
.\.venv\Scripts\python.exe start_local.py
```

Open http://127.0.0.1:8000. Press Ctrl+C to stop.

## CLI commands

```powershell
# One live check via SerpAPI (requires SERPAPI_KEY)
.\.venv\Scripts\rank-agent.exe check --config project.json

# Local fixture demo using Chrome — no network, no API key needed
.\.venv\Scripts\rank-agent.exe demo

# Submit to durable queue
.\.venv\Scripts\rank-agent.exe enqueue --config project.json --key jkm-2026-09-10
.\.venv\Scripts\rank-agent.exe worker --once
.\.venv\Scripts\rank-agent.exe status JOB_ID

# Run the daily auto-scheduler (enqueues one job per day at RANK_SCHEDULER_HOUR:RANK_SCHEDULER_MINUTE UTC)
.\.venv\Scripts\rank-agent.exe scheduler --config project.json

# Verify evidence integrity
.\.venv\Scripts\rank-agent.exe verify data/evidence/RUN_ID

# Run tests
.\.venv\Scripts\python.exe -m pytest -q
```

## Production deployment (Docker + PostgreSQL + TLS)

Requirements: a server with Docker, a domain pointing to it, and ports 80/443 open.

1. **Copy and fill the production env file:**

```bash
cp .env.production.example .env.production
# Edit .env.production — fill in SERPAPI_KEY, POSTGRES_PASSWORD, RANK_API_TOKEN, etc.
```

2. **Edit the `Caddyfile`** — replace `your-domain.com` with your actual domain.

3. **Start all services:**

```bash
docker compose up -d
```

Services started:
| Service | Role |
|---------|------|
| `db` | PostgreSQL 16 with persistent volume |
| `worker` | Processes queued ranking jobs |
| `scheduler` | Enqueues one job per day automatically |
| `api` | Authenticated REST API (port 8080 → exposed via Caddy) |
| `caddy` | TLS termination + reverse proxy (ports 80 + 443) |

4. **Check health:**

```bash
curl https://your-domain.com/health
```

5. **Submit a check and read result:**

```bash
curl -X POST https://your-domain.com/jobs \
  -H "Authorization: Bearer <RANK_API_TOKEN>" \
  -H "Idempotency-Key: jkm-2026-09-10" \
  -H "Content-Type: application/json" \
  -d @project.json

curl https://your-domain.com/jobs/<JOB_ID> \
  -H "Authorization: Bearer <RANK_API_TOKEN>"
```

## Optional: S3 evidence backup

Set `RANK_S3_BUCKET` in your env file. After each check, all evidence files are uploaded to `s3://<bucket>/<prefix>/<run_id>/`. Requires `boto3` (`pip install boto3`) and AWS credentials (IAM role on EC2 recommended; or `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` env vars).

## How it works

**Production checks** use SerpAPI (`source: "serpapi"`): the API returns structured organic results without direct browser automation, complying with Google's automated-traffic policy. Evidence is the raw API JSON response + SHA-256 manifest + HTML report. Location is confirmed via the `location` field returned by SerpAPI (`location_verified: true` when the requested city matches).

**Demo mode** (`rank-agent demo`) uses a local Chrome fixture and is clearly labelled `source: "fixture"` — it is never a live Google ranking.

ADK exposes `submit_ranking_check` and `get_ranking_check`. A GPT-6 Astra model explains stored observations; it does not calculate rankings.

The queue uses SQLite locally and PostgreSQL in production. Jobs have idempotency keys, compare-and-swap leases, and up to 3 retries for transient errors.

## Meaning of results

| Status | Meaning |
|--------|---------|
| found | Target matched in observed organic results |
| not_found_within_depth | All requested positions checked; no match |
| incomplete | Fewer results than requested; absence not established |
| blocked | API error, quota exceeded, or access barrier |
| extraction_failed | Ambiguous, duplicate, or empty result page |
| capture_failed | Configuration error; no verified rank published |

## API endpoints

```
POST /jobs             Submit a check (Bearer token + Idempotency-Key header)
GET  /jobs/{job_id}    Read stored job/result (Bearer token)
GET  /evidence/{run_id}/{filename}  Download evidence file (Bearer token)
GET  /health           Process liveness (public)
```

## Reinstall

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

References: [SerpAPI Python](https://serpapi.com/integrations/python), [ADK Python](https://adk.dev/get-started/python/), [Playwright](https://playwright.dev/python/docs/browsers).
