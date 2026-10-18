"""Turn fetched HTML / PDF / text into compact readable text for the model."""

from __future__ import annotations

import html as html_lib
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup, Comment

MAX_LINKS = 60
LINK_KEYWORDS = re.compile(
    r"apply|application|register|registration|sign[ -]?up|eligib|rule|faq|deadline|prize|award|fund|grant|"
    r"contact|term|submit|submission|guideline|timeline|schedule|about|track|challenge|judg|criteria|fee|"
    r"partner|sponsor|program|cohort|international|student",
    re.I,
)
BLOCK_TAGS = [
    "p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "ul", "ol",
    "table", "tbody", "thead", "tr", "dl", "dt", "dd", "blockquote", "pre", "figure", "figcaption",
    "address", "details", "summary", "form", "fieldset", "label", "hr", "caption",
]  # fmt: skip
DROP_TAGS = ["script", "style", "noscript", "svg", "template", "iframe", "canvas", "object", "embed", "head"]
JS_HINTS = re.compile(
    r"enable javascript|javascript is (disabled|required)|requires javascript|turn on javascript|"
    r"you need to enable javascript",
    re.I,
)
CHALLENGE_HINTS = re.compile(
    r"cf-browser-verification|challenge-platform|cf_chl_|g-recaptcha|h-captcha|hcaptcha|px-captcha|"
    r"perimeterx|datadome|verify (that )?you are (a )?human|are you a robot|attention required! \| cloudflare|"
    r"just a moment\.\.\.|checking your browser|unusual traffic from your computer|captcha",
    re.I,
)


@dataclass
class Extracted:
    title: str | None
    text: str
    links: list[dict] = field(default_factory=list)
    script_count: int = 0
    description: str | None = None


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "\n").replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = []
    previous = None
    for line in text.split("\n"):
        line = line.strip()
        if line and line == previous:
            continue
        lines.append(line)
        previous = line if line else previous
    return "\n".join(lines).strip()


def html_to_text(markup: str, base_url: str) -> Extracted:
    soup = BeautifulSoup(markup, "html.parser")
    title = None
    if soup.title and soup.title.string:
        title = normalize_whitespace(soup.title.get_text(" "))[:300] or None
    description = None
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        description = normalize_whitespace(str(meta.get("content")))[:500]
    script_count = len(soup.find_all("script"))

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(DROP_TAGS):
        if not tag.decomposed:
            tag.decompose()

    links: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", "")).strip()
        if not href or href.startswith(("javascript:", "#", "tel:", "data:")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        if not absolute.startswith(("http://", "https://", "mailto:")):
            continue
        label = normalize_whitespace(a.get_text(" "))[:120]
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append({"url": absolute, "text": label})

    for br in soup.find_all("br"):
        br.replace_with("\n")
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.insert_before("\n\n" + "#" * level + " ")
            h.insert_after("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
        li.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" | ")
    for block in soup.find_all(BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")

    body = soup.body or soup
    text = normalize_whitespace(html_lib.unescape(body.get_text(" ")))
    if description and description not in text[:2000]:
        text = f"{description}\n\n{text}"
    ranked = sorted(links, key=lambda l: 0 if LINK_KEYWORDS.search(l["text"] + " " + l["url"]) else 1)
    return Extracted(title=title, text=text, links=ranked[:MAX_LINKS], script_count=script_count, description=description)


def pdf_to_text(data: bytes, max_pages: int = 60) -> tuple[str | None, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    title = None
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)[:300]
    except Exception:  # broken metadata is common; the text still matters
        title = None
    parts = []
    for index, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        parts.append(f"[page {index}]\n{page_text}")
    if len(reader.pages) > max_pages:
        parts.append(f"[... {len(reader.pages) - max_pages} more pages not extracted]")
    return title, normalize_whitespace("\n\n".join(parts))


def looks_like_challenge(status: int | None, text: str, title: str | None) -> bool:
    sample = f"{title or ''}\n{text[:4000]}"
    if CHALLENGE_HINTS.search(sample):
        # A long article that merely mentions "captcha" is not a challenge page.
        return status in (401, 403, 429, 503) or len(text) < 3000
    return False


def needs_javascript(extracted: Extracted) -> bool:
    visible = len(extracted.text)
    if visible < 400 and (extracted.script_count >= 3 or JS_HINTS.search(extracted.text)):
        return True
    return bool(JS_HINTS.search(extracted.text[:1000])) and visible < 1500
