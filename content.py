"""Feed content parsing: RSS/RDF/Atom entries, dates, and HTML feed discovery.

Pure parsing — no network, no database — so it's easy to test in isolation.
"""
import re
import html
from datetime import datetime, timezone
from urllib.parse import urljoin
import xml.etree.ElementTree as ET
from defusedxml.ElementTree import fromstring as _safe_fromstring
from email.utils import parsedate_to_datetime

# Feed XML is attacker-influenced (any subscribed feed, or a discovered site), so
# parse it defused: entity definitions and external references are rejected, which
# neutralises "billion laughs" expansion and XXE. ET is kept only for ParseError
# and for building OPML on export.


def _local(tag):
    """Strip an XML namespace, e.g. '{http://...}title' -> 'title'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    """Parse an RFC-822 or ISO-8601 date string into a normalised UTC ISO string."""
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _find_thumbnail(entry, raw_summary):
    """media:thumbnail / media:content / image enclosure / first <img> in the body."""
    for c in entry.iter():
        if _local(c.tag) == "thumbnail" and c.get("url"):
            return c.get("url")
    for c in entry.iter():
        lt = _local(c.tag)
        if lt == "content" and c.get("url") and (
            c.get("medium") == "image" or c.get("type", "").startswith("image")
        ):
            return c.get("url")
        if lt == "enclosure" and c.get("type", "").startswith("image") and c.get("url"):
            return c.get("url")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', raw_summary or "", re.I)
    return m.group(1) if m else ""


def _parse_entry(e):
    children = list(e)

    def first(*names):
        for c in children:
            if _local(c.tag) in names:
                return c
        return None

    title = _text(first("title"))

    # Link: RSS puts it in element text, Atom in an href attribute (prefer rel=alternate).
    link = ""
    for c in children:
        if _local(c.tag) != "link":
            continue
        href = c.get("href")
        if href:
            if c.get("rel", "alternate") == "alternate" or not link:
                link = href
        elif (c.text or "").strip():
            link = c.text.strip()

    raw_summary = _text(first("encoded", "description", "summary", "content"))
    summary = strip_html(raw_summary)

    author = ""
    a = first("creator", "author")
    if a is not None:
        names = [x for x in list(a) if _local(x.tag) == "name"]
        author = _text(names[0]) if names else _text(a)

    published = parse_date(_text(first("pubDate", "published", "date", "updated")))
    guid = _text(first("guid", "id")) or link or title

    return {
        "guid": guid, "title": title, "url": link, "summary": summary,
        "author": author, "published": published,
        "thumbnail": _find_thumbnail(e, raw_summary),
    }


def parse_feed(data):
    """Return a list of item dicts from RSS, RDF or Atom XML."""
    root = _safe_fromstring(data)
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    out = []
    for e in entries:
        it = _parse_entry(e)
        if it["title"] or it["url"]:
            out.append(it)
    return out


def _feed_title(data):
    try:
        root = _safe_fromstring(data)
    except (ET.ParseError, ValueError):
        return ""
    # The channel/feed title is the first <title> that isn't inside an item/entry.
    for el in root.iter():
        if _local(el.tag) in ("item", "entry"):
            break
        if _local(el.tag) == "title" and (el.text or "").strip():
            return el.text.strip()
    return ""


def _html_title(data):
    m = re.search(r"<title[^>]*>(.*?)</title>", data.decode("utf-8", "replace"), re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""


def _discover_feed(data, base):
    text = data.decode("utf-8", "replace")
    for m in re.finditer(r"<link\b[^>]*>", text, re.I):
        tag = m.group(0)
        if re.search(r'rel=["\']?alternate', tag, re.I) and \
           re.search(r'type=["\']?application/(rss|atom)\+xml', tag, re.I):
            hm = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
            if hm:
                return urljoin(base, html.unescape(hm.group(1)))
    return None
