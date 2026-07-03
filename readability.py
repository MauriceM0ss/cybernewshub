"""Reader view (article preview).

A tiny readability-style extractor: parse the page, pick the container with the
most paragraph text, then re-emit a sanitised, whitelisted subset of HTML. Pure
stdlib — no new dependencies. Output is safe to drop into the DOM with innerHTML.
"""
import html
from urllib.parse import urljoin, urlparse
from html.parser import HTMLParser

_READER_DROP = {"script", "style", "noscript", "svg", "form", "nav", "header",
                "footer", "aside", "button", "iframe", "object", "embed",
                "select", "input", "textarea", "head", "link", "meta", "label"}
# Void elements have no end tag, so they must never put the parser into "skip until
# close" mode (otherwise a dropped <meta>/<link> would swallow the rest of the page).
_READER_VOID = {"img", "br", "hr", "area", "base", "col", "embed", "input",
                "link", "meta", "param", "source", "track", "wbr"}
# Tags re-emitted as themselves (attributes stripped); everything else is unwrapped.
_READER_BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
                 "blockquote", "pre", "code", "strong", "em", "b", "i", "u",
                 "figure", "figcaption", "table", "thead", "tbody", "tr", "td",
                 "th", "div", "hr"}
_READER_TEXT = {"p", "blockquote"}      # tags whose text counts as "real" article prose


class _DOMBuilder(HTMLParser):
    """Build a lightweight nested-dict tree, dropping unwanted subtrees as we go."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.skip_depth = 0
        self.skip_tag = None

    def handle_starttag(self, tag, attrs):
        if self.skip_depth:
            if tag == self.skip_tag and tag not in _READER_VOID:
                self.skip_depth += 1
            return
        if tag in _READER_DROP:
            if tag not in _READER_VOID:
                self.skip_depth, self.skip_tag = 1, tag
            return
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in _READER_VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self.skip_depth or tag in _READER_DROP:
            return
        self.stack[-1]["children"].append({"tag": tag, "attrs": dict(attrs), "children": []})

    def handle_endtag(self, tag):
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if not self.skip_depth:
                    self.skip_tag = None
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.skip_depth:
            self.stack[-1]["children"].append(data)


def _text_len(node):
    n = 0
    for c in node["children"]:
        n += len(c.strip()) if isinstance(c, str) else _text_len(c)
    return n


def _para_score(node):
    """Length of text that sits inside genuine paragraph-ish tags."""
    total = 0
    for c in node["children"]:
        if isinstance(c, str):
            continue
        if c["tag"] in _READER_TEXT:
            total += _text_len(c)
        else:
            total += _para_score(c)
    return total


def _link_len(node):
    """Length of text that sits inside <a> tags — used to penalise nav/menu blocks."""
    total = 0
    for c in node["children"]:
        if isinstance(c, str):
            continue
        total += _text_len(c) if c["tag"] == "a" else _link_len(c)
    return total


def _collect_candidates(node, out):
    for c in node["children"]:
        if isinstance(c, str):
            continue
        if c["tag"] in ("div", "article", "section", "main"):
            out.append(c)
        _collect_candidates(c, out)


def _safe_url(value, base):
    try:
        absolute = urljoin(base, (value or "").strip())
    except ValueError:
        return None
    return absolute if urlparse(absolute).scheme in ("http", "https") else None


def _serialize(node, base, depth=0):
    if depth > 80:                       # guard against pathological nesting
        return ""
    out = []
    for c in node["children"]:
        if isinstance(c, str):
            out.append(html.escape(c))
            continue
        tag = c["tag"]
        if tag == "img":
            src = (c["attrs"].get("data-src") or c["attrs"].get("data-original")
                   or c["attrs"].get("src") or "")
            if not src:
                ss = c["attrs"].get("srcset") or c["attrs"].get("data-srcset") or ""
                src = ss.split(",")[0].strip().split(" ")[0] if ss else ""
            url = _safe_url(src, base)
            if url:
                alt = html.escape(c["attrs"].get("alt", ""))
                out.append(f'<img src="{html.escape(url)}" alt="{alt}" loading="lazy">')
            continue
        if tag == "br":
            out.append("<br>")
            continue
        inner = _serialize(c, base, depth + 1)
        if tag == "a":
            url = _safe_url(c["attrs"].get("href", ""), base)
            out.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener nofollow">{inner}</a>'
                       if url else inner)
        elif tag in _READER_BLOCK:
            out.append(f"<{tag}>{inner}</{tag}>" if tag != "hr" else "<hr>")
        else:                            # span, section, article, main, … → unwrap
            out.append(inner)
    return "".join(out)


def extract_readable(doc, base_url):
    """Return sanitised article HTML, or '' if nothing readable was found."""
    builder = _DOMBuilder()
    try:
        builder.feed(doc)
    except Exception:
        pass
    candidates = []
    _collect_candidates(builder.root, candidates)
    best, best_score = None, 0
    for c in candidates:
        score = _para_score(c) - _link_len(c)   # penalise link-heavy nav/menu blocks
        if c["tag"] == "article":
            score = int(score * 1.4)            # prefer semantic <article> wrappers
        if score > best_score:
            best, best_score = c, score
    if best is not None and best_score >= 140:
        return _serialize(best, base_url)[:300_000]
    # No clear article container — fall back to the whole page only if it actually
    # holds prose (some sites put <p>s straight in <body>). Otherwise give up so the
    # caller shows a clean "open in new tab" message instead of nav/menu junk.
    if _para_score(builder.root) - _link_len(builder.root) >= 250:
        return _serialize(builder.root, base_url)[:300_000]
    return ""
