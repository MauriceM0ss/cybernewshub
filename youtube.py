"""YouTube channel resolution and Shorts detection.

A YouTube channel/playlist/@handle URL resolves to its videos.xml feed (no API
key). Shorts carry no RSS flag, so we probe the /shorts/<id> URL: an actual Short
returns 200, a regular video redirects to /watch.
"""
import re
import html
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor

import config
import net
import content
import database


def _youtube_feed(url):
    """Resolve a YouTube channel/playlist/handle URL to its videos.xml feed."""
    p = urlparse(url)
    qs = parse_qs(p.query)
    m = re.search(r"/channel/(UC[\w-]+)", p.path)
    if m:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}", ""
    if "list" in qs:
        return f"https://www.youtube.com/feeds/videos.xml?playlist_id={qs['list'][0]}", ""
    # @handle, /user/, /c/, /watch — find the channel's *own* id in the page HTML.
    # Prefer externalId / canonical link: a bare "channelId" can be a featured or
    # cross-linked channel (e.g. a creator's second channel) rather than this one.
    text = net.http_get(url).decode("utf-8", "replace")
    m = (re.search(r'"externalId":"(UC[\w-]+)"', text)
         or re.search(r'rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[\w-]+)"', text)
         or re.search(r'<meta property="og:url" content="https://www\.youtube\.com/channel/(UC[\w-]+)"', text)
         or re.search(r'"channelId":"(UC[\w-]+)"', text)
         or re.search(r"channel_id=(UC[\w-]+)", text))
    if not m:
        return None, ""
    tm = re.search(r'<meta property="og:title" content="([^"]+)"', text)
    return (f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}",
            html.unescape(tm.group(1)) if tm else "")


class _NoRedirect(HTTPRedirectHandler):
    """An opener that does NOT follow redirects, so we can read the raw status."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_SHORTS_OPENER = build_opener(_NoRedirect)


def _youtube_video_id(guid, url):
    if guid and guid.startswith("yt:video:"):
        return guid.rsplit(":", 1)[-1]
    for pat in (r"[?&]v=([\w-]{6,})", r"youtu\.be/([\w-]{6,})", r"/shorts/([\w-]{6,})"):
        m = re.search(pat, url or "")
        if m:
            return m.group(1)
    return None


def _is_short(video_id):
    """A Shorts URL returns 200 for an actual Short, but redirects to /watch for a
    regular video. Unknown/errors -> False so we never hide real videos by mistake.
    The SOCS cookie skips YouTube's GDPR consent wall, which would otherwise redirect
    every request to consent.youtube.com and make everything look like a non-Short."""
    req = Request(f"https://www.youtube.com/shorts/{video_id}",
                  headers={"User-Agent": config.USER_AGENT, "Cookie": "SOCS=CAI; CONSENT=YES+1"})
    try:
        with _SHORTS_OPENER.open(req, timeout=12) as r:
            return getattr(r, "status", r.getcode()) == 200
    except (HTTPError, URLError, OSError):
        return False


def classify_shorts(limit=300):
    """Probe YouTube items not yet classified and record whether each is a Short."""
    with database.db() as conn:
        rows = conn.execute(
            "SELECT i.id, i.guid, i.url FROM items i JOIN feeds f ON f.id = i.feed_id "
            "WHERE f.type='youtube' AND i.is_short IS NULL LIMIT ?", (limit,)).fetchall()
    if not rows:
        return

    def work(r):
        vid = _youtube_video_id(r["guid"], r["url"])
        return r["id"], (1 if (vid and _is_short(vid)) else 0)

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(work, rows))
    with database.db() as conn:
        for iid, val in results:
            conn.execute("UPDATE items SET is_short=? WHERE id=?", (val, iid))
