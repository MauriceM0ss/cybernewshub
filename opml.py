"""OPML import/export — the taxonomy interchange format (Category > Subject > Feed)."""
import xml.etree.ElementTree as ET
from defusedxml.ElementTree import fromstring as _safe_fromstring

import database


def _ensure_category(conn, name):
    name = (name or "Imported").strip() or "Imported"
    conn.execute("INSERT OR IGNORE INTO categories (name, sort_order) VALUES "
                 "(?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM categories))", (name,))
    return conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()["id"]


def _ensure_subject(conn, category_id, name):
    name = (name or "General").strip() or "General"
    conn.execute("INSERT OR IGNORE INTO subjects (category_id, name, sort_order) VALUES "
                 "(?, ?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM subjects WHERE category_id=?))",
                 (category_id, name, category_id))
    return conn.execute("SELECT id FROM subjects WHERE category_id=? AND name=?",
                        (category_id, name)).fetchone()["id"]


def _add_outline_feed(conn, subject_id, o):
    xml_url = o.get("xmlUrl")
    if not xml_url:
        return 0
    title = (o.get("text") or o.get("title") or "").strip()
    ftype = "youtube" if "youtube.com/feeds" in xml_url else "rss"
    site = o.get("htmlUrl") or ""
    cur = conn.execute(
        "INSERT OR IGNORE INTO feeds (subject_id, title, url, site_url, type, sort_order) "
        "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM feeds WHERE subject_id=?))",
        (subject_id, title, xml_url, site, ftype, subject_id))
    return cur.rowcount


def import_opml(data):
    """Import an OPML tree. Two nesting levels map to category → subject; feeds found
    above the subject level fall back to a 'General' subject."""
    root = _safe_fromstring(data)   # uploaded file — defused against entity/XXE abuse
    body = root.find("body")
    if body is None:
        return 0
    added = 0
    with database.db() as conn:
        for cat in body.findall("outline"):
            if cat.get("xmlUrl"):                             # stray top-level feed
                sid = _ensure_subject(conn, _ensure_category(conn, "Imported"), "General")
                added += _add_outline_feed(conn, sid, cat)
                continue
            cid = _ensure_category(conn, cat.get("text") or cat.get("title"))
            children = cat.findall("outline")
            direct = [o for o in children if o.get("xmlUrl")]
            if direct:
                sid = _ensure_subject(conn, cid, "General")
                for o in direct:
                    added += _add_outline_feed(conn, sid, o)
            for sub in [o for o in children if not o.get("xmlUrl")]:
                sid = _ensure_subject(conn, cid, sub.get("text") or sub.get("title"))
                for o in sub.findall("outline"):
                    added += _add_outline_feed(conn, sid, o)
    return added


def export_opml():
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "CyberNewsHub feeds"
    body = ET.SubElement(opml, "body")
    with database.db() as conn:
        cats = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
        for c in cats:
            cat_ol = ET.SubElement(body, "outline", text=c["name"], title=c["name"])
            subs = conn.execute("SELECT * FROM subjects WHERE category_id=? "
                                "ORDER BY sort_order, name", (c["id"],)).fetchall()
            for s in subs:
                sub_ol = ET.SubElement(cat_ol, "outline", text=s["name"], title=s["name"])
                feeds = conn.execute("SELECT * FROM feeds WHERE subject_id=? "
                                     "ORDER BY sort_order, title", (s["id"],)).fetchall()
                for f in feeds:
                    ET.SubElement(sub_ol, "outline", type=f["type"],
                                  text=f["title"] or f["url"], title=f["title"] or f["url"],
                                  xmlUrl=f["url"], htmlUrl=f["site_url"])
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(opml, encoding="utf-8")
