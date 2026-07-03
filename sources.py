"""Source resolution (RSS sniff / HTML auto-discovery / YouTube) and the refresh
engine that fetches feeds, upserts items and prunes old ones.
"""
import re
import time
import random
import logging
import threading
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor

import config
from config import SourceError, now_iso
import net
import content
import youtube
import database

log = logging.getLogger(__name__)


def resolve_source(url):
    """Turn any pasted URL into {type, feed_url, title, site_url}."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    host = urlparse(url).netloc.lower()

    if "youtube.com" in host or "youtu.be" in host:
        feed_url, title = youtube._youtube_feed(url)
        if feed_url:
            return {"type": "youtube", "feed_url": feed_url,
                    "title": title or content._feed_title(net.http_get(feed_url)), "site_url": url}
        raise SourceError("Couldn't find a YouTube channel for that link.")

    data, ctype = net.http_get(url, with_ctype=True)
    head = data[:1500].lstrip()
    looks_xml = head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head or b"<rdf" in head[:200]
    if looks_xml or "xml" in ctype.lower():
        title = content._feed_title(data)
        if title or b"<item" in data[:20000] or b"<entry" in data[:20000]:
            return {"type": "rss", "feed_url": url, "title": title, "site_url": url}

    feed_url = content._discover_feed(data, url)
    if feed_url:
        try:
            fdata = net.http_get(feed_url)
            return {"type": "rss", "feed_url": feed_url,
                    "title": content._feed_title(fdata) or content._html_title(data), "site_url": url}
        except Exception as e:                                # noqa: BLE001
            raise SourceError(f"Found a feed link but couldn't load it: {e}")

    raise SourceError("No RSS/Atom feed found on that page.")


# ── Refreshing ────────────────────────────────────────────────────────────────
def _record_feed_failure(feed, exc):
    """Persist a failed fetch. YouTube throttling shows up as a 404 (or 429/5xx) on its
    RSS endpoint, so for YouTube feeds we treat those as transient and hold off surfacing
    an error (the ⚠ triangle) until the feed has failed YT_FAIL_THRESHOLD times in a row —
    a real outage keeps failing and eventually shows; a passing throttle never does.
    Returns (0, raw_error_message)."""
    msg  = str(exc)
    code = exc.code if isinstance(exc, HTTPError) else None
    is_yt = feed.get("type") == "youtube"
    if isinstance(exc, HTTPError):
        transient = is_yt and code in (404, 429, 500, 502, 503)
    else:                                                     # timeouts, DNS, refused conns
        transient = is_yt and isinstance(exc, (URLError, OSError))

    with database.db() as conn:
        row   = conn.execute("SELECT fail_count FROM feeds WHERE id=?", (feed["id"],)).fetchone()
        fails = ((row["fail_count"] if row else 0) or 0) + 1
        if transient and fails < config.YT_FAIL_THRESHOLD:
            surfaced = ""                                     # likely just throttling — stay quiet
        elif is_yt and code in (404, 429):
            surfaced = (f"HTTP {code} on YouTube's RSS endpoint — failed {fails}× in a row. "
                        f"Usually means this server is being rate-limited by YouTube (it often "
                        f"recovers on its own); if it persists for days the channel may have been "
                        f"removed or recreated.")
        else:
            surfaced = msg[:300]
        conn.execute("UPDATE feeds SET last_error=?, fail_count=?, last_fetched=? WHERE id=?",
                     (surfaced, fails, now_iso(), feed["id"]))
    return 0, msg


def refresh_feed(feed):
    """Fetch one feed, upsert its items, prune old ones. Returns (added, error)."""
    try:
        entries = content.parse_feed(net.http_get(feed["url"]))
    except Exception as e:                                    # noqa: BLE001
        return _record_feed_failure(feed, e)

    added = 0
    with database.db() as conn:
        for it in entries:
            cur = conn.execute(
                "INSERT OR IGNORE INTO items "
                "(feed_id, guid, title, url, summary, author, published, thumbnail, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (feed["id"], it["guid"], it["title"][:500], it["url"], it["summary"][:2000],
                 it["author"][:200], it["published"], it["thumbnail"], now_iso()))
            added += cur.rowcount
        conn.execute("UPDATE feeds SET last_error='', fail_count=0, last_fetched=? WHERE id=?",
                     (now_iso(), feed["id"]))
        # Keep starred & read-later items forever; trim the rest to the most recent ITEM_RETENTION.
        conn.execute(
            "DELETE FROM items WHERE feed_id=? AND starred=0 AND read_later=0 AND id NOT IN "
            "(SELECT id FROM items WHERE feed_id=? "
            " ORDER BY COALESCE(published, fetched_at) DESC LIMIT ?)",
            (feed["id"], feed["id"], config.ITEM_RETENTION))
    return added, None


_refresh_busy = threading.Lock()
_refresh_state = {"running": False, "last_run": "", "summary": None}


def do_refresh():
    """Refresh every feed concurrently. Only one run happens at a time."""
    if not _refresh_busy.acquire(blocking=False):
        return
    try:
        _refresh_state["running"] = True
        with database.db() as conn:
            feeds = [dict(r) for r in conn.execute("SELECT id, url, type FROM feeds")]
        yt   = [f for f in feeds if f["type"] == "youtube"]
        rest = [f for f in feeds if f["type"] != "youtube"]
        summary = {"feeds": len(feeds), "ok": 0, "failed": 0, "added": 0}

        def tally(added, err):
            if err:
                summary["failed"] += 1
            else:
                summary["ok"] += 1
                summary["added"] += added

        # Plain RSS/Atom: fetch concurrently — ordinary sites don't throttle like YouTube does.
        if rest:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for added, err in ex.map(refresh_feed, rest):
                    tally(added, err)
        # YouTube: fetch sequentially with a small jittered delay so we stay under its
        # per-IP RSS rate limit (which it enforces by returning 404 on bursts).
        for i, f in enumerate(yt):
            if i:
                time.sleep(config.YT_FETCH_DELAY + random.uniform(0, 0.5))
            tally(*refresh_feed(f))
        youtube.classify_shorts()                             # tag any new YouTube items
        _refresh_state["summary"] = summary
        _refresh_state["last_run"] = now_iso()
        database.set_setting("last_refresh", _refresh_state["last_run"])   # survive restarts
    finally:
        _refresh_state["running"] = False
        _refresh_busy.release()


def _refresh_loop():
    do_refresh()                                              # initial fill on startup
    elapsed = 0
    while True:
        time.sleep(30)                                        # tick; re-read the interval each time
        elapsed += 30
        if elapsed < max(60, database.get_refresh_minutes() * 60):
            continue
        elapsed = 0
        try:
            do_refresh()
        except Exception as e:                                # noqa: BLE001
            log.warning("Background refresh failed: %s", e)
