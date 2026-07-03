"""CyberNewsHub — a self-hosted RSS / YouTube / website news aggregator.

This module wires the Flask app and HTTP routes together; the domain logic lives
in focused modules:

    config      env-derived settings + shared primitives (SourceError, now_iso)
    database    SQLite connection, schema/migrations, key/value settings helpers
    net         outbound HTTP (single capped GET) + favicon URLs
    content     RSS/Atom/RDF parsing, date handling, HTML feed discovery
    readability reader-view extractor + HTML sanitizer (safe for innerHTML)
    youtube     channel resolution + Shorts detection
    sources     source resolution + the refresh engine
    opml        OPML import/export
    brief       AI daily brief (Claude API)
"""
import io
import hmac
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError

from flask import Flask, render_template, request, jsonify, send_file, Response

import config
import database
import net
import content
import readability
import youtube
import sources
import opml
import brief

# ── Back-compat / test facade: expose the most-used names at module scope so
#    ``import app`` keeps working as a single public surface. ─────────────────
from config import SourceError, now_iso
from database import (db, init_db, get_setting, set_setting, hide_shorts_enabled,
                      get_refresh_minutes, get_watchlist, watchlist_sql_filter,
                      api_key_from_env, get_api_key)
from net import http_get, favicon_for
from content import (parse_feed, parse_date, strip_html, _feed_title,
                     _discover_feed, _html_title)
from readability import extract_readable, _safe_url
from youtube import _youtube_feed, _youtube_video_id, classify_shorts
from sources import resolve_source, refresh_feed, do_refresh, _refresh_loop
from opml import import_opml, export_opml
from brief import generate_brief, _collect_brief_items

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cybernewshub")

DB_PATH = config.DB_PATH           # retained for reference; the source of truth is config.DB_PATH
SEED_OPML = config.SEED_OPML
_STATIC = Path(__file__).parent / "static"

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe for the container healthcheck. No auth required."""
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return jsonify({"status": "ok"})
    except Exception as e:                                    # noqa: BLE001
        log.error("Healthcheck DB error: %s", e)
        return jsonify({"status": "error"}), 503


@app.before_request
def _security_gate():
    if request.path == "/healthz":       # probe must work without creds/origin
        return None
    request._started = time.monotonic()
    """Optional Basic auth + a lightweight CSRF (same-origin) check.

    Auth is off unless AUTH_USER/AUTH_PASSWORD are set, preserving the open
    localhost default. The CSRF check rejects state-changing requests whose
    Origin/Referer is a different site — a browser always sends one of these on a
    cross-site POST, while non-browser clients (curl) send neither and are allowed.
    """
    # ── Optional HTTP Basic auth ──────────────────────────────────────────────
    if config.AUTH_USER and config.AUTH_PASSWORD:
        auth = request.authorization
        ok = (auth and auth.type == "basic"
              and hmac.compare_digest(auth.username or "", config.AUTH_USER)
              and hmac.compare_digest(auth.password or "", config.AUTH_PASSWORD))
        if not ok:
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="CyberNewsHub"'})

    # ── CSRF: same-origin guard for mutating requests ─────────────────────────
    if request.method in _MUTATING:
        origin = request.headers.get("Origin")
        source = origin or request.headers.get("Referer")
        if source:
            if urlparse(source).netloc != request.host:
                return jsonify({"error": "Cross-origin request blocked."}), 403


@app.after_request
def _access_log(resp):
    """One concise access-log line per request (method, path, status, ms)."""
    if request.path == "/healthz":
        return resp
    started = getattr(request, "_started", None)
    ms = f"{(time.monotonic() - started) * 1000:.0f}ms" if started else "-"
    log.info("%s %s -> %s (%s)", request.method, request.full_path.rstrip("?"),
             resp.status_code, ms)
    return resp


@app.errorhandler(Exception)
def _log_unhandled(e):
    """Log unhandled exceptions with a stack trace, but let Flask's normal HTTP
    error handling (404/400/…) pass through unchanged."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error on %s %s", request.method, request.path)
    return jsonify({"error": "Internal server error."}), 500


# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("feed.html")


# ── PWA: manifest + service worker (served from root so the SW controls the whole app) ──
@app.route("/manifest.webmanifest")
def web_manifest():
    return send_file(_STATIC / "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    resp = send_file(_STATIC / "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"     # allow root scope from a /static-built file
    resp.headers["Cache-Control"] = "no-cache"       # always revalidate the SW itself
    return resp


# ── API: sidebar tree ───────────────────────────────────────────────────────────
@app.route("/api/tree")
def api_tree():
    with db() as conn:
        cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
        subs = conn.execute("SELECT * FROM subjects ORDER BY sort_order, name").fetchall()
        feeds = conn.execute("SELECT * FROM feeds ORDER BY sort_order, title COLLATE NOCASE").fetchall()
        hide = " AND is_short IS NOT 1" if hide_shorts_enabled() else ""
        counts = {r["feed_id"]: r["n"] for r in conn.execute(
            "SELECT feed_id, COUNT(*) AS n FROM items WHERE read=0" + hide + " GROUP BY feed_id")}
        totals = conn.execute(
            "SELECT COUNT(*) AS total, SUM(read=0) AS unread, SUM(starred=1) AS starred, "
            "       SUM(read_later=1) AS read_later "
            "FROM items" + (" WHERE is_short IS NOT 1" if hide_shorts_enabled() else "")).fetchone()
        fclause, fparams = watchlist_sql_filter()
        flagged = conn.execute(
            "SELECT COUNT(*) AS n FROM items i WHERE " + fclause
            + (" AND i.is_short IS NOT 1" if hide_shorts_enabled() else ""), fparams).fetchone()["n"]

    feeds_by_subject = {}
    for f in feeds:
        feeds_by_subject.setdefault(f["subject_id"], []).append({
            "id": f["id"], "title": f["title"] or f["url"], "type": f["type"],
            "unread": counts.get(f["id"], 0),
            "favicon": favicon_for(f["site_url"], f["url"]),
            "last_error": f["last_error"], "last_fetched": f["last_fetched"],
        })
    subs_by_cat = {}
    for s in subs:
        sfeeds = feeds_by_subject.get(s["id"], [])
        subs_by_cat.setdefault(s["category_id"], []).append({
            "id": s["id"], "name": s["name"], "feeds": sfeeds,
            "unread": sum(f["unread"] for f in sfeeds),
        })
    tree = []
    for c in cats:
        csubs = subs_by_cat.get(c["id"], [])
        tree.append({
            "id": c["id"], "name": c["name"], "subjects": csubs,
            "unread": sum(s["unread"] for s in csubs),
        })
    return jsonify({
        "tree": tree,
        "totals": {"all": totals["total"] or 0,
                   "unread": totals["unread"] or 0,
                   "starred": totals["starred"] or 0,
                   "read_later": totals["read_later"] or 0,
                   "flagged": flagged or 0},
    })


# ── API: items ──────────────────────────────────────────────────────────────────
@app.route("/api/items")
def api_items():
    scope = request.args.get("scope", "all")
    sid = request.args.get("id")
    q = request.args.get("q", "").strip()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0

    where, params = [], []
    if scope == "feed":
        where.append("f.id = ?"); params.append(sid)
    elif scope == "subject":
        where.append("f.subject_id = ?"); params.append(sid)
    elif scope == "category":
        where.append("s.category_id = ?"); params.append(sid)
    elif scope == "starred":
        where.append("i.starred = 1")
    elif scope == "read_later":
        where.append("i.read_later = 1")
    elif scope == "flagged":
        fclause, fparams = watchlist_sql_filter()
        where.append(fclause); params += fparams
    elif scope == "unread":
        where.append("i.read = 0")
    if q:
        where.append("(i.title LIKE ? OR i.summary LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if hide_shorts_enabled():
        where.append("i.is_short IS NOT 1")
    wsql = (" WHERE " + " AND ".join(where)) if where else ""

    sql = (
        "SELECT i.id, i.title, i.url, i.summary, i.author, i.published, i.thumbnail, "
        "       i.read, i.starred, i.read_later, i.fetched_at, "
        "       f.title AS feed_title, f.type AS feed_type, "
        "       s.name AS subject_name, c.name AS category_name "
        "FROM items i "
        "JOIN feeds f ON f.id = i.feed_id "
        "JOIN subjects s ON s.id = f.subject_id "
        "JOIN categories c ON c.id = s.category_id "
        f"{wsql} "
        "ORDER BY COALESCE(i.published, i.fetched_at) DESC "
        "LIMIT ? OFFSET ?"
    )
    with db() as conn:
        rows = conn.execute(sql, params + [config.PAGE_SIZE + 1, offset]).fetchall()
    has_more = len(rows) > config.PAGE_SIZE
    return jsonify({
        "items": [dict(r) for r in rows[:config.PAGE_SIZE]],
        "has_more": has_more,
    })


@app.route("/api/items/<int:iid>/preview")
def api_item_preview(iid):
    """Fetch an article server-side and return a clean reader-view of its body."""
    with db() as conn:
        row = conn.execute("SELECT url FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Article not found."}), 404
    url = row["url"]
    if not url or urlparse(url).scheme not in ("http", "https"):
        return jsonify({"ok": False, "error": "This article has no previewable link."}), 400
    try:
        raw, ctype = net.http_get(url, with_ctype=True)
    except (HTTPError, URLError, ValueError, OSError) as e:
        return jsonify({"ok": False, "error": f"Couldn't reach the page ({e})."}), 502
    if "html" not in (ctype or "").lower() and not raw.lstrip()[:1] == b"<":
        return jsonify({"ok": False, "error": "This link isn't a readable web page."}), 415
    try:
        body = readability.extract_readable(raw.decode("utf-8", "replace"), url)
    except Exception:
        body = ""
    if not body.strip():
        return jsonify({"ok": False, "error": "Couldn't extract readable content."}), 200
    return jsonify({"ok": True, "html": body})


@app.route("/api/items/<int:iid>/read", methods=["POST"])
def api_item_read(iid):
    read = 1 if request.get_json(force=True).get("read", True) else 0
    with db() as conn:
        conn.execute("UPDATE items SET read=? WHERE id=?", (read, iid))
    return jsonify({"ok": True, "read": bool(read)})


@app.route("/api/items/<int:iid>/star", methods=["POST"])
def api_item_star(iid):
    star = 1 if request.get_json(force=True).get("starred", True) else 0
    with db() as conn:
        conn.execute("UPDATE items SET starred=? WHERE id=?", (star, iid))
    return jsonify({"ok": True, "starred": bool(star)})


@app.route("/api/items/<int:iid>/read-later", methods=["POST"])
def api_item_read_later(iid):
    later = 1 if request.get_json(force=True).get("read_later", True) else 0
    with db() as conn:
        conn.execute("UPDATE items SET read_later=? WHERE id=?", (later, iid))
    return jsonify({"ok": True, "read_later": bool(later)})


@app.route("/api/mark-read", methods=["POST"])
def api_mark_read():
    d = request.get_json(force=True)
    scope, sid = d.get("scope", "all"), d.get("id")
    with db() as conn:
        if scope == "feed":
            conn.execute("UPDATE items SET read=1 WHERE feed_id=?", (sid,))
        elif scope == "subject":
            conn.execute("UPDATE items SET read=1 WHERE feed_id IN "
                         "(SELECT id FROM feeds WHERE subject_id=?)", (sid,))
        elif scope == "category":
            conn.execute("UPDATE items SET read=1 WHERE feed_id IN "
                         "(SELECT f.id FROM feeds f JOIN subjects s ON s.id=f.subject_id "
                         " WHERE s.category_id=?)", (sid,))
        elif scope == "starred":
            conn.execute("UPDATE items SET read=1 WHERE starred=1")
        elif scope == "read_later":
            conn.execute("UPDATE items SET read=1 WHERE read_later=1")
        elif scope == "flagged":
            fclause, fparams = watchlist_sql_filter()
            conn.execute("UPDATE items SET read=1 WHERE id IN "
                         "(SELECT i.id FROM items i WHERE " + fclause + ")", fparams)
        elif scope == "unread":
            conn.execute("UPDATE items SET read=1 WHERE read=0")
        else:
            conn.execute("UPDATE items SET read=1")
    return jsonify({"ok": True})


# ── API: refresh ────────────────────────────────────────────────────────────────
@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=sources.do_refresh, daemon=True).start()
    return jsonify({"running": True})


@app.route("/api/refresh", methods=["GET"])
def api_refresh_status():
    state = dict(sources._refresh_state)
    if not state["last_run"]:                  # not refreshed yet this run — use persisted value
        state["last_run"] = get_setting("last_refresh", "") or ""
    return jsonify(state)


# ── API: settings ───────────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({
        "hide_shorts": hide_shorts_enabled(),
        "refresh_minutes": get_refresh_minutes(),
        "refresh_choices": list(config.REFRESH_CHOICES),
        "watchlist": get_watchlist(),
        "has_api_key": bool(get_api_key()),   # never return the key itself
        "api_key_env": api_key_from_env(),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    d = request.get_json(force=True)
    if "hide_shorts" in d:
        set_setting("hide_shorts", "1" if d["hide_shorts"] else "0")
    if "refresh_minutes" in d:
        try:
            m = int(d["refresh_minutes"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid refresh interval."}), 400
        if m not in config.REFRESH_CHOICES:
            return jsonify({"error": "Invalid refresh interval."}), 400
        set_setting("refresh_minutes", m)
    if "watchlist" in d:
        terms = d["watchlist"]
        if isinstance(terms, str):
            terms = terms.splitlines()
        if not isinstance(terms, list):
            return jsonify({"error": "Invalid watchlist."}), 400
        clean, seen = [], set()
        for t in terms:
            t = str(t).strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                clean.append(t)
        set_setting("watchlist", "\n".join(clean[:100]))   # cap to keep it sane
    # Anthropic API key — write-only: saved here, never returned by GET.
    if d.get("clear_api_key"):
        set_setting("anthropic_api_key", "")
    elif "anthropic_api_key" in d:
        v = (d["anthropic_api_key"] or "").strip()
        if v:                                               # ignore blanks so other saves don't wipe it
            set_setting("anthropic_api_key", v)
    return jsonify({"ok": True})


# ── API: watchlist hits (for desktop notifications) ──────────────────────────────
@app.route("/api/watch-hits")
def api_watch_hits():
    """New items (id greater than `since`) whose title/summary matches a watchlist
    term. The client tracks the last id it saw and notifies on anything newer."""
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    terms = get_watchlist()
    with db() as conn:
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM items").fetchone()["m"]
        items = []
        if terms:
            clause = " OR ".join(["(i.title LIKE ? OR i.summary LIKE ?)"] * len(terms))
            params = [since]
            for t in terms:
                params += [f"%{t}%", f"%{t}%"]
            hide = " AND i.is_short IS NOT 1" if hide_shorts_enabled() else ""
            rows = conn.execute(
                "SELECT i.id, i.title, i.url, f.title AS feed_title "
                "FROM items i JOIN feeds f ON f.id = i.feed_id "
                f"WHERE i.id > ? AND ({clause}){hide} "
                "ORDER BY i.id DESC LIMIT 20", params).fetchall()
            items = [dict(r) for r in rows]
    return jsonify({"max_id": max_id, "items": items})


# ── API: AI daily brief ─────────────────────────────────────────────────────────
@app.route("/api/brief", methods=["GET"])
def api_brief_get():
    with db() as conn:
        r = conn.execute("SELECT content, created_at, item_count, day FROM briefs "
                         "ORDER BY day DESC LIMIT 1").fetchone()
    return jsonify({
        "has_api_key": bool(get_api_key()),
        "today": datetime.now(timezone.utc).date().isoformat(),
        "brief": dict(r) if r else None,
    })


@app.route("/api/brief", methods=["POST"])
def api_brief_post():
    result, err = brief.generate_brief()
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"brief": result})


# ── API: categories ─────────────────────────────────────────────────────────────
@app.route("/api/categories", methods=["POST"])
def api_category_create():
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required."}), 400
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO categories (name, sort_order) VALUES "
                "(?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM categories))", (name,))
        except database.sqlite3.IntegrityError:
            return jsonify({"error": "That category already exists."}), 400
        cid = cur.lastrowid
        # Give the category a default subject so it's immediately usable as a feed target.
        conn.execute("INSERT INTO subjects (category_id, name, sort_order) VALUES (?, 'General', 0)", (cid,))
    return jsonify({"id": cid, "name": name})


@app.route("/api/categories/<int:cid>", methods=["PUT"])
def api_category_update(cid):
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required."}), 400
    with db() as conn:
        try:
            conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
        except database.sqlite3.IntegrityError:
            return jsonify({"error": "That category already exists."}), 400
    return jsonify({"ok": True})


@app.route("/api/categories/<int:cid>", methods=["DELETE"])
def api_category_delete(cid):
    with db() as conn:
        conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    return jsonify({"ok": True})


# ── API: subjects ───────────────────────────────────────────────────────────────
@app.route("/api/subjects", methods=["POST"])
def api_subject_create():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    try:
        cid = int(d["category_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing category."}), 400
    if not name:
        return jsonify({"error": "Name required."}), 400
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO subjects (category_id, name, sort_order) VALUES "
                "(?, ?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM subjects WHERE category_id=?))",
                (cid, name, cid))
        except database.sqlite3.IntegrityError:
            return jsonify({"error": "That subject already exists here."}), 400
    return jsonify({"id": cur.lastrowid, "name": name, "category_id": cid})


@app.route("/api/subjects/<int:sid>", methods=["PUT"])
def api_subject_update(sid):
    d = request.get_json(force=True)
    fields, vals = [], []
    if "name" in d:
        name = (d["name"] or "").strip()
        if not name:
            return jsonify({"error": "Name required."}), 400
        fields.append("name=?"); vals.append(name)
    if "category_id" in d:
        fields.append("category_id=?"); vals.append(int(d["category_id"]))
    if not fields:
        return jsonify({"error": "Nothing to update."}), 400
    vals.append(sid)
    with db() as conn:
        try:
            conn.execute(f"UPDATE subjects SET {', '.join(fields)} WHERE id=?", vals)
        except database.sqlite3.IntegrityError:
            return jsonify({"error": "That subject already exists here."}), 400
    return jsonify({"ok": True})


@app.route("/api/subjects/<int:sid>", methods=["DELETE"])
def api_subject_delete(sid):
    with db() as conn:
        conn.execute("DELETE FROM subjects WHERE id=?", (sid,))
    return jsonify({"ok": True})


# ── API: feeds ──────────────────────────────────────────────────────────────────
@app.route("/api/feeds", methods=["GET"])
def api_feeds_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT f.*, s.name AS subject_name, c.name AS category_name, c.id AS category_id "
            "FROM feeds f JOIN subjects s ON s.id=f.subject_id "
            "JOIN categories c ON c.id=s.category_id "
            "ORDER BY c.sort_order, s.sort_order, f.sort_order, f.title COLLATE NOCASE").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/discover", methods=["POST"])
def api_discover():
    url = (request.get_json(force=True).get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL required."}), 400
    try:
        return jsonify(sources.resolve_source(url))
    except SourceError as e:
        return jsonify({"error": str(e)}), 200
    except Exception as e:                                    # noqa: BLE001
        return jsonify({"error": f"Couldn't reach that URL: {e}"}), 200


@app.route("/api/feeds", methods=["POST"])
def api_feed_create():
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    try:
        subject_id = int(d["subject_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Pick a subject for this feed."}), 400
    if not url:
        return jsonify({"error": "URL required."}), 400
    try:
        info = sources.resolve_source(url)
    except SourceError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                    # noqa: BLE001
        return jsonify({"error": f"Couldn't reach that URL: {e}"}), 400

    title = (d.get("title") or info["title"] or info["feed_url"]).strip()
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO feeds (subject_id, title, url, site_url, type, sort_order) "
                "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM feeds WHERE subject_id=?))",
                (subject_id, title, info["feed_url"], info["site_url"], info["type"], subject_id))
            fid = cur.lastrowid
        except database.sqlite3.IntegrityError:
            return jsonify({"error": "That feed is already in your list."}), 400
        feed = dict(conn.execute("SELECT id, url, title, type FROM feeds WHERE id=?", (fid,)).fetchone())
    sources.refresh_feed(feed)                                # pull its first items right away
    if info["type"] == "youtube":                             # classify Shorts off the request path
        threading.Thread(target=youtube.classify_shorts, daemon=True).start()
    return jsonify({"id": fid, "title": title, "type": info["type"]})


@app.route("/api/feeds/<int:fid>", methods=["PUT"])
def api_feed_update(fid):
    d = request.get_json(force=True)
    fields, vals = [], []
    if "title" in d:
        fields.append("title=?"); vals.append((d["title"] or "").strip())
    if "subject_id" in d:
        fields.append("subject_id=?"); vals.append(int(d["subject_id"]))
    if not fields:
        return jsonify({"error": "Nothing to update."}), 400
    vals.append(fid)
    with db() as conn:
        conn.execute(f"UPDATE feeds SET {', '.join(fields)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/feeds/<int:fid>", methods=["DELETE"])
def api_feed_delete(fid):
    with db() as conn:
        conn.execute("DELETE FROM feeds WHERE id=?", (fid,))
    return jsonify({"ok": True})


@app.route("/api/feeds/<int:fid>/retry", methods=["POST"])
def api_feed_retry(fid):
    """Re-fetch a single feed on demand (used by the diagnostic dialog)."""
    with db() as conn:
        row = conn.execute("SELECT id, url, type FROM feeds WHERE id=?", (fid,)).fetchone()
    if not row:
        return jsonify({"error": "Feed not found."}), 404
    added, err = sources.refresh_feed(dict(row))
    if not err:
        youtube.classify_shorts()                             # tag any new YouTube items
    with db() as conn:
        f = conn.execute("SELECT last_error, last_fetched FROM feeds WHERE id=?", (fid,)).fetchone()
    return jsonify({"ok": err is None, "added": added, "error": err,
                    "last_error": f["last_error"], "last_fetched": f["last_fetched"]})


@app.route("/api/feeds/health")
def api_feeds_health():
    """Per-feed status for the health dashboard: item counts, last fetch, error state."""
    with db() as conn:
        rows = conn.execute(
            "SELECT f.id, f.title, f.url, f.type, f.last_fetched, f.last_error, f.fail_count, "
            "       s.name AS subject, c.name AS category, "
            "       (SELECT COUNT(*) FROM items WHERE feed_id=f.id) AS items, "
            "       (SELECT COUNT(*) FROM items WHERE feed_id=f.id AND read=0) AS unread "
            "FROM feeds f "
            "JOIN subjects s ON s.id = f.subject_id "
            "JOIN categories c ON c.id = s.category_id "
            "ORDER BY (f.last_error != '') DESC, f.fail_count DESC, f.title COLLATE NOCASE"
        ).fetchall()
    feeds = []
    for r in rows:
        fd = dict(r)
        fd["title"] = fd["title"] or fd["url"]
        feeds.append(fd)
    summary = {
        "total": len(feeds),
        "erroring": sum(1 for fd in feeds if fd["last_error"]),
        "stale": sum(1 for fd in feeds if not fd["last_fetched"]),
    }
    return jsonify({"feeds": feeds, "summary": summary})


# ── API: OPML + reset ───────────────────────────────────────────────────────────
@app.route("/api/opml/import", methods=["POST"])
def api_opml_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        added = opml.import_opml(f.read())
    except (content.ET.ParseError, ValueError):
        # ValueError covers defusedxml's entity/DTD/external-reference rejections.
        return jsonify({"error": "That doesn't look like a valid OPML file."}), 400
    threading.Thread(target=sources.do_refresh, daemon=True).start()
    return jsonify({"ok": True, "added": added})


@app.route("/api/opml/export")
def api_opml_export():
    return send_file(io.BytesIO(opml.export_opml()), as_attachment=True,
                     download_name="cybernewshub-feeds.opml", mimetype="text/x-opml")


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with db() as conn:
        conn.execute("DELETE FROM categories")               # cascades subjects → feeds → items
    return jsonify({"ok": True})


init_db()
threading.Thread(target=_refresh_loop, daemon=True).start()

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
