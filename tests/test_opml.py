"""Characterization tests for OPML import/export and the refresh/retention logic."""
import net

OPML = b"""<?xml version="1.0"?>
<opml version="2.0">
  <head><title>My feeds</title></head>
  <body>
    <outline text="Security">
      <outline text="Blogs">
        <outline type="rss" text="Krebs" xmlUrl="https://krebs.example/feed"
                 htmlUrl="https://krebs.example"/>
      </outline>
    </outline>
    <outline text="Video">
      <outline type="rss" text="Chan"
               xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id=UCabc"/>
    </outline>
  </body>
</opml>"""


def test_import_opml_builds_tree(app_mod):
    added = app_mod.import_opml(OPML)
    assert added == 2
    with app_mod.db() as conn:
        cats = [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY name")]
        assert cats == ["Security", "Video"]
        # A feed directly under a category lands in a 'General' subject.
        gen = conn.execute(
            "SELECT s.name FROM subjects s JOIN categories c ON c.id=s.category_id "
            "WHERE c.name='Video'").fetchone()["name"]
        assert gen == "General"
        # xmlUrl containing youtube.com/feeds is typed as youtube.
        yt = conn.execute(
            "SELECT type FROM feeds WHERE url LIKE '%youtube%'").fetchone()["type"]
        assert yt == "youtube"


def test_import_opml_is_idempotent(app_mod):
    app_mod.import_opml(OPML)
    added_again = app_mod.import_opml(OPML)
    assert added_again == 0  # feeds are UNIQUE by url; re-import adds nothing


def test_export_roundtrips(app_mod):
    app_mod.import_opml(OPML)
    out = app_mod.export_opml()
    assert out.startswith(b'<?xml version="1.0" encoding="utf-8"?>')
    assert b"CyberNewsHub feeds" in out
    assert b'xmlUrl="https://krebs.example/feed"' in out


def test_export_download_headers(client, seeded):
    r = client.get("/api/opml/export")
    assert r.status_code == 200
    assert "cybernewshub-feeds.opml" in r.headers["Content-Disposition"]


def test_opml_import_endpoint_rejects_junk(client):
    import io
    r = client.post("/api/opml/import",
                    data={"file": (io.BytesIO(b"not opml"), "x.opml")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_refresh_feed_inserts_and_prunes(app_mod, seeded, monkeypatch):
    feed_xml = b"""<rss><channel>
      <item><title>Fresh item</title><link>https://example.com/fresh</link>
            <guid>fresh-1</guid></item>
    </channel></rss>"""
    monkeypatch.setattr(net, "http_get", lambda url: feed_xml)
    feed = {"id": seeded["fid"], "url": "https://example.com/feed.xml", "type": "rss"}
    added, err = app_mod.refresh_feed(feed)
    assert err is None
    assert added == 1
    with app_mod.db() as conn:
        titles = [r["title"] for r in conn.execute(
            "SELECT title FROM items WHERE feed_id=? AND guid='fresh-1'",
            (seeded["fid"],))]
    assert titles == ["Fresh item"]


def test_refresh_feed_records_failure(app_mod, seeded, monkeypatch):
    def boom(url):
        raise app_mod.URLError("connection refused")

    monkeypatch.setattr(net, "http_get", boom)
    feed = {"id": seeded["fid"], "url": "https://example.com/feed.xml", "type": "rss"}
    added, err = app_mod.refresh_feed(feed)
    assert added == 0
    assert err is not None
    with app_mod.db() as conn:
        row = conn.execute("SELECT fail_count, last_error FROM feeds WHERE id=?",
                           (seeded["fid"],)).fetchone()
    # A non-YouTube feed surfaces the error immediately.
    assert row["fail_count"] == 1
    assert row["last_error"] != ""
