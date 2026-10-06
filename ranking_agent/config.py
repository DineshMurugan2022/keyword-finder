import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"), override=False)

# ── Shared timing constants ────────────────────────────────────────────────
# Worker kills a job after WORKER_TIMEOUT_SECONDS; the DB lease is kept longer
# so a crashed worker does not immediately steal the slot from a retry attempt.
WORKER_TIMEOUT_SECONDS: int = 600
LEASE_DURATION_SECONDS: int = WORKER_TIMEOUT_SECONDS + 300  # 900 s

# Default LLM model used by the agent (referenced by agent.py).
DEFAULT_AGENT_MODEL: str = "gemini-2.0-flash"


def data_dir(create: bool = True) -> Path:
    """Return (and optionally create) the configured data directory.

    Pass create=False for read-only callers to avoid creating directories
    as a side-effect of checking whether a path exists.
    """
    path = Path(os.getenv("RANK_DATA_DIR", "./data")).resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def database_url() -> str:
    return os.getenv("RANK_DATABASE_URL") or f"sqlite:///{data_dir() / 'rankings.db'}"


def headless() -> bool:
    return os.getenv("RANK_HEADLESS", "true").lower() != "false"


def serpapi_key() -> str:
    """Return the SerpAPI key. Raises RuntimeError if not configured."""
    key = os.getenv("SERPAPI_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SERPAPI_KEY is not set. Add it to ranking_agent/.env to run production checks."
        )
    return key


def s3_bucket() -> str | None:
    """Return the S3 bucket name, or None if S3 upload is disabled."""
    return os.getenv("RANK_S3_BUCKET", "").strip() or None


def s3_prefix() -> str:
    return os.getenv("RANK_S3_PREFIX", "ranking-evidence").strip()


def scheduler_hour() -> str:
    return os.getenv("RANK_SCHEDULER_HOUR", "2").strip()


def scheduler_minute() -> str:
    return os.getenv("RANK_SCHEDULER_MINUTE", "0").strip()

