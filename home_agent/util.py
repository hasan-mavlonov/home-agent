"""Small shared helpers: URL keys, text normalisation, time zones, secret scrubbing."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|igshid|_hs.*|spm)$", re.I)
QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        "−": "-", " ": " ", "​": ""})  # fmt: skip


def get_zone(name: str | None, fallback: str = "Asia/Shanghai") -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def local_now(tz: str) -> datetime:
    return datetime.now(get_zone(tz))


def normalize_url(url: str) -> str:
    """Canonical form for comparing URLs: lower-case host without www, no fragment,
    no tracking parameters, no trailing slash, http and https treated alike."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = re.sub(r"/+$", "", parts.path or "") or ""
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    return urlunsplit(("https", host + port, path, query, ""))


def url_key(url: str) -> str:
    return normalize_url(url).removeprefix("https://")


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(QUOTES).lower()
    text = re.sub(r"[*_`#>|]+", " ", text)  # markdown and table punctuation from extraction
    return re.sub(r"\s+", " ", text).strip()


def alnum_only(text: str) -> str:
    return re.sub(r"[\W_]+", "", normalize_for_match(text))


def quote_in_text(quote: str, text: str) -> bool:
    """True if `quote` (as copied by the model) appears in `text` (the fetched page)."""
    q = normalize_for_match(quote)
    if len(q) < 4:
        return False
    if q in normalize_for_match(text):
        return True
    qa = alnum_only(quote)
    return len(qa) >= 6 and qa in alnum_only(text)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


URL_PATTERN = re.compile(r"https?://[^\s<>\"'\)\]]+")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def urls_in(text: str) -> list[str]:
    return [u.rstrip(".,;:") for u in URL_PATTERN.findall(text or "")]
