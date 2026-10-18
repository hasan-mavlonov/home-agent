"""Safe Markdown rendering for model output (which may quote untrusted web content).

Raw HTML is disabled, so any HTML in the text is shown escaped. Links are limited to
http(s), mailto and in-app (#/...) targets and open in a new tab without a referrer.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])
_BARE_URL = re.compile(r"(?<![(<\[\"'=])\b(https?://[^\s<>()\[\]\"']+[^\s<>()\[\]\"'.,;:!?])")
_SAFE_HREF = re.compile(r"^(https?://|mailto:|#/)", re.I)


def _link_open(self, tokens, idx, options, env):
    token = tokens[idx]
    href = token.attrGet("href") or ""
    if not _SAFE_HREF.match(href):
        token.attrSet("href", "#")
    elif not href.startswith("#/"):
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer nofollow")
    return self.renderToken(tokens, idx, options, env)


_md.add_render_rule("link_open", _link_open)


def _autolink(text: str) -> str:
    out = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        out.append(line if in_fence else _BARE_URL.sub(r"<\1>", line))
    return "\n".join(out)


def render_markdown(text: str | None) -> str:
    if not text:
        return ""
    return _md.render(_autolink(text))
