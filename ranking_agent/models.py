import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_host(value: str) -> str:
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("Use a public website domain or HTTP(S) URL without credentials")
    host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host:
        raise ValueError("Enter a valid domain, for example example.com")
    return host


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    website: str
    keyword: str = Field(min_length=1, max_length=300)
    country: str = "in"
    language: str = "en"
    location: str = Field(default="", max_length=120)
    max_results: int = Field(default=30, ge=1, le=100)
    include_subdomains: bool = True

    @field_validator("website")
    @classmethod
    def website_host(cls, value: str) -> str:
        return normalize_host(value)

    @field_validator("keyword")
    @classmethod
    def keyword_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Keyword cannot be blank")
        return value

    @field_validator("country", "language")
    @classmethod
    def locale_code(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z]{2}", value):
            raise ValueError("Use a two-letter country/language code")
        return value.lower()


def matches_domain(url: str, domain: str, include_subdomains: bool = True) -> bool:
    try:
        host = normalize_host(url)
    except (ValueError, UnicodeError):
        return False
    return host == domain or (include_subdomains and host.endswith("." + domain))
