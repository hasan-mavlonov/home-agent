"""Small shared helpers: URL keys, text normalisation, time zones, secret scrubbing."""

from __future__ import annotations

import bisect
import functools
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


# "...", "[...]", "(...)" in a normalised quote (NFKC already turned "…" into "...")
ELLIPSIS = re.compile(r"\s*[\[(]?\s*(?:\.\s*){3,}[\])]?\s*")
PART_EDGES = " ,;:.!?-()[]\"'"
QUOTE_GAP = 500  # max characters skipped between the pieces of a "..." quote


MAX_PIECE_HITS = 10_000  # bounds the work on repetitive pages; such a generic piece is no evidence anyway


def _in_order(parts: list[str], text: str, max_gap: int) -> bool:
    """Every part occurs in `text` in order, each starting at most `max_gap` characters after
    the end of the previous one. One pass per part over its occurrences, so a repetitive
    page costs at most MAX_PIECE_HITS steps per part."""
    if len(parts) == 1:
        return parts[0] in text
    if not all(part in text for part in parts):
        return False
    ends: list[int] | None = None  # sorted end positions where the previous part can finish
    for part in parts:
        reachable = []
        pos, hits = text.find(part), 0
        while pos != -1:
            hits += 1
            if hits > MAX_PIECE_HITS:
                return False
            if ends is None:
                reachable.append(pos + len(part))
            else:
                index = bisect.bisect_right(ends, pos)  # ends[index - 1] is the closest end at or before pos
                if index and pos - ends[index - 1] <= max_gap:
                    reachable.append(pos + len(part))
            pos = text.find(part, pos + 1)
        if not reachable:
            return False
        ends = reachable
    return True


def quote_in_text(quote: str, text: str) -> bool:
    """True if `quote` (as copied by the model) appears in `text` (the fetched page).

    "..." in a quote marks skipped words. Every piece must then be on the page, in order and
    close together, so pieces stitched from unrelated parts of a page do not count."""
    q = normalize_for_match(quote)
    if len(q) < 4:
        return False
    page, page_alnum = _page_forms(text)
    if q in page:
        return True
    parts = [p for p in (s.strip(PART_EDGES) for s in ELLIPSIS.split(q)) if p]
    if len(parts) > 1 and sum(map(len, parts)) >= 4 and _in_order(parts, page, QUOTE_GAP):
        return True
    alnum_parts = [a for a in (alnum_only(p) for p in parts) if a]
    if sum(map(len, alnum_parts)) < 6:
        return False
    # dropping spaces and punctuation shortens distances; shrink the allowed gap to match this page
    gap = QUOTE_GAP * len(page_alnum) // max(1, len(page))
    return _in_order(alnum_parts, page_alnum, gap)


@functools.lru_cache(maxsize=8)
def _page_forms(text: str) -> tuple[str, str]:
    """Normalised and letters-and-digits-only page text. Cached: one save checks many quotes
    against the same few pages."""
    return normalize_for_match(text), alnum_only(text)


def quote_in_text(quote: str, text: str) -> bool:
    """True if `quote` (as copied by the model) appears in `text` (the fetched page).

    A quote shortened with "..." matches only if every part appears on the page."""
    parts = [p for p in ELLIPSIS.split(quote or "") if p.strip()]
    if len(parts) > 1:
        return all(_fragment_in_text(p, text) for p in parts)
    return _fragment_in_text(quote, text)


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
