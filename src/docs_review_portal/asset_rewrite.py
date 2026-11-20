"""Rewrites root relative paths inside a preview's scripts and stylesheets.

A preview is served under a path such as /my-preview/, but the site was built to live at
the root, so a path like "/get-docker/desktop/" would leave the preview. Links in the HTML
are handled by rewrite_docs_domain_urls. This module covers the places that function
doesn't read: JavaScript files, scripts inside pages, CSS files, and page attributes other
than href, src, action and poster, such as spec-url or the value of an option.

A path is only rewritten when the file it points at really exists in that version of the
site. That keeps strings that merely start with a slash, such as a separator, a regular
expression, or the address of a service outside the site, exactly as they were written.
Paths that the code builds while it runs can't be found by reading it, and are left to the
server's fallback.
"""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import unquote

from docs_review_portal.config import RESERVED_ROOT_SEGMENTS

# Bump when the rewriting changes, so files cached by browsers or the server are rebuilt.
REWRITER_VERSION = "1"

REWRITABLE_EXTENSIONS = (".js", ".mjs", ".css")

# Files larger than this are sent as they are.
MAX_REWRITE_BYTES = 8 * 1024 * 1024

# A quoted string that starts with a single slash and holds a path. The string may end there,
# or continue with a query, a fragment, or a template placeholder, as in `/docs/#${name}`.
_JS_PATH = re.compile(
    r"""(?P<quote>["'`])(?P<path>/(?!/)[A-Za-z0-9_\-./%~@+]+)(?=[?#"'`]|\$\{)"""
)

_CSS_URL = re.compile(
    r"""url\(\s*(?P<quote>["']?)(?P<path>/(?!/)[^)"'\s?#]+)(?P<tail>[?#][^)"']*)?(?P=quote)\s*\)""",
    re.IGNORECASE,
)
_CSS_IMPORT = re.compile(
    r"""@import\s+(?P<quote>["'])(?P<path>/(?!/)[^"'?#]+)(?P<tail>[?#][^"']*)?(?P=quote)""",
    re.IGNORECASE,
)

# Scripts in a page. Only the kinds that hold code or paths are read.
_SCRIPT_BLOCK = re.compile(
    r"(?P<open><script\b(?P<attrs>[^>]*)>)(?P<body>.*?)(?P<close></script\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_TYPE = re.compile(r"""\btype\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
_READABLE_SCRIPT_TYPES = {
    "",
    "module",
    "text/javascript",
    "application/javascript",
    "text/ecmascript",
    "application/ecmascript",
    "importmap",
    "speculationrules",
}

# A fingerprint ties a file to its exact bytes, so a file that was rewritten no longer matches.
_TAG_WITH_INTEGRITY = re.compile(r"<(?:script|link)\b[^>]*\bintegrity\s*=[^>]*>", re.IGNORECASE)
_INTEGRITY_ATTR = re.compile(r"""\s+integrity\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_EXTERNAL_URL = re.compile(r"""\b(?:src|href)\s*=\s*["']?(?:https?:)?//""", re.IGNORECASE)

# Attributes written inside a tag. Only a value that is a plain path, with an optional query or
# fragment, is looked at, so code samples, expressions and lists are never touched.
_TAG = re.compile(r"<[A-Za-z][^<>]*>")
_ATTRIBUTE = re.compile(
    r"""(?P<lead>[\s"'])(?P<name>[A-Za-z_][-A-Za-z0-9_:.]*)(?P<eq>\s*=\s*)"""
    r"""(?:"(?P<dq>/[^"]*)"|'(?P<sq>/[^']*)'|(?P<uq>/[^\s"'`>]*))"""
)
_PLAIN_PATH_VALUE = re.compile(r"^(?P<path>/(?!/)[A-Za-z0-9_\-./%~@+]+)(?P<tail>[?#]\S*)?$")
# These are already rewritten by rewrite_docs_domain_urls.
_ALREADY_HANDLED_ATTRIBUTES = {"href", "src", "action", "poster"}

Exists = Callable[[str], bool]


def _base(prefix: str) -> str:
    base = prefix if prefix.startswith("/") else f"/{prefix}"
    return base.rstrip("/")


def _is_site_path(path: str, base: str, exists: Exists) -> bool:
    """True when path is a root relative path to a file or page that this site has,
    and not already under the preview's own prefix or one of the portal's own routes."""
    if len(path) < 2:
        return False
    if path == base or path.startswith(f"{base}/"):
        return False
    segments = path.split("/")
    if segments[1].lower() in RESERVED_ROOT_SEGMENTS:
        return False
    rel = unquote(path[1:])
    if any(part == ".." for part in rel.split("/")):
        return False
    if rel.endswith("/"):
        candidates = [f"{rel}index.html"]
    else:
        candidates = [rel, f"{rel}/index.html"]
    return any(exists(candidate) for candidate in candidates)


def rewrite_script_text(text: str, prefix: str, exists: Exists) -> str:
    """Put the preview's prefix on the paths this site has, in the strings of a script."""
    if "/" not in text:
        return text
    base = _base(prefix)

    def replace(match: re.Match) -> str:
        path = match.group("path")
        if not _is_site_path(path, base, exists):
            return match.group(0)
        return f"{match.group('quote')}{base}{path}"

    return _JS_PATH.sub(replace, text)


def rewrite_css_text(text: str, prefix: str, exists: Exists) -> str:
    """Put the preview's prefix on the paths this site has, in url(...) and @import."""
    if "/" not in text:
        return text
    base = _base(prefix)

    def replace_url(match: re.Match) -> str:
        path = match.group("path")
        if not _is_site_path(path, base, exists):
            return match.group(0)
        quote = match.group("quote")
        return f"url({quote}{base}{path}{match.group('tail') or ''}{quote})"

    def replace_import(match: re.Match) -> str:
        path = match.group("path")
        if not _is_site_path(path, base, exists):
            return match.group(0)
        quote = match.group("quote")
        return f"@import {quote}{base}{path}{match.group('tail') or ''}{quote}"

    return _CSS_IMPORT.sub(replace_import, _CSS_URL.sub(replace_url, text))


def _rewrite_tag_attributes(tag: str, base: str, exists: Exists) -> str:
    def replace(match: re.Match) -> str:
        if match.group("name").lower() in _ALREADY_HANDLED_ATTRIBUTES:
            return match.group(0)
        value = match.group("dq") or match.group("sq") or match.group("uq") or ""
        plain = _PLAIN_PATH_VALUE.match(value)
        if not plain or not _is_site_path(plain.group("path"), base, exists):
            return match.group(0)
        rewritten = f"{base}{value}"
        if match.group("dq") is not None:
            quoted = f'"{rewritten}"'
        elif match.group("sq") is not None:
            quoted = f"'{rewritten}'"
        else:
            quoted = rewritten
        return f"{match.group('lead')}{match.group('name')}{match.group('eq')}{quoted}"

    return _ATTRIBUTE.sub(replace, tag)


def rewrite_page_extras(html_text: str, prefix: str, exists: Exists) -> str:
    """The parts of a page that rewrite_docs_domain_urls leaves alone: scripts written inside
    it, and attributes other than href, src, action and poster. Also drops the integrity
    fingerprint from the page's own scripts and stylesheets, which may have been rewritten
    as files."""
    base = _base(prefix)

    def replace_script(match: re.Match) -> str:
        attrs = match.group("attrs")
        if re.search(r"\bsrc\s*=", attrs, re.IGNORECASE):
            return match.group(0)
        type_match = _SCRIPT_TYPE.search(attrs)
        script_type = ""
        if type_match:
            script_type = (type_match.group(1) or type_match.group(2) or type_match.group(3) or "").strip().lower()
        if script_type not in _READABLE_SCRIPT_TYPES:
            return match.group(0)
        body = rewrite_script_text(match.group("body"), prefix, exists)
        return f"{match.group('open')}{body}{match.group('close')}"

    def drop_integrity(match: re.Match) -> str:
        tag = match.group(0)
        if _EXTERNAL_URL.search(tag):
            return tag
        return _INTEGRITY_ATTR.sub("", tag)

    html_text = _SCRIPT_BLOCK.sub(replace_script, html_text)
    html_text = _TAG.sub(lambda m: _rewrite_tag_attributes(m.group(0), base, exists), html_text)
    return _TAG_WITH_INTEGRITY.sub(drop_integrity, html_text)


def rewrite_asset(content: bytes, rel_path: str, prefix: str, exists: Exists) -> bytes:
    """Rewrite a script or stylesheet file. Anything that can't be read as text, or that
    needs no change, is returned as it came."""
    lowered = rel_path.lower()
    if len(content) > MAX_REWRITE_BYTES or not lowered.endswith(REWRITABLE_EXTENSIONS):
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    if lowered.endswith(".css"):
        rewritten = rewrite_css_text(text, prefix, exists)
    else:
        rewritten = rewrite_script_text(text, prefix, exists)
    if rewritten == text:
        return content
    return rewritten.encode("utf-8")
