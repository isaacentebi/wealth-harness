"""The pages as one text: each page's /static CSS and JS put back inline, so tests read what the browser runs.

Every <link rel="stylesheet" href="/static/X.css"> becomes one combined <style> block at the first link, and every
<script src="/static/X.js"></script> one combined <script> block at the first tag, byte for byte as the files hold
them. A page that still carries its CSS or JS inline reads unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path

WEALTH = Path(__file__).resolve().parent.parent / "wealth"
STATIC = WEALTH / "static"

_CSS = re.compile(r'(?m)^([ \t]*)<link rel="stylesheet" href="/static/([\w.-]+\.css)">\n')
_JS = re.compile(r'(?m)^([ \t]*)<script src="/static/([\w.-]+\.js)"></script>\n')


def _inline(page: str, pattern: re.Pattern[str], tag: str) -> str:
    found = list(pattern.finditer(page))
    if not found:
        return page
    indent = found[0].group(1)
    body = "".join((STATIC / m.group(2)).read_text(encoding="utf-8") for m in found)
    block = f"{indent}<{tag}>\n{body}{indent}</{tag}>\n"
    out, at = [], 0
    for i, m in enumerate(found):
        out.append(page[at:m.start()])
        if i == 0:
            out.append(block)
        at = m.end()
    out.append(page[at:])
    return "".join(out)


def page_text(name: str) -> str:
    """wealth/<name>.html with its /static CSS and JS inlined; name is "chat", "profile" or "review"."""
    page = (WEALTH / f"{name.removesuffix('.html')}.html").read_text(encoding="utf-8")
    return _inline(_inline(page, _CSS, "style"), _JS, "script")
