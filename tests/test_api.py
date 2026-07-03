"""Characterization tests for the HTTP API via Flask's test client."""
import net


def test_index_ok(client):
    r = client.get("/")
    assert r.status_code == 200


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_healthz_bypasses_auth(client, monkeypatch):
    import config
    monkeypatch.setattr(config, "AUTH_USER", "alice")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret")
    # Every other route needs auth now, but the probe must still answer.
    assert client.get("/api/tree").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_manifest_and_sw(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["Service-Worker-Allowed"] == "/"


def test_tree_shape(client, seeded):
    data = client.get("/api/tree").get_json()
    assert set(data) == {"tree", "totals"}
    assert data["totals"]["all"] == 3
    assert data["totals"]["unread"] == 2
    assert data["totals"]["starred"] == 1
    assert data["totals"]["read_later"] == 1
    cat = data["tree"][0]
    assert cat["name"] == "News"
    assert cat["unread"] == 2
    feed = cat["subjects"][0]["feeds"][0]
    assert feed["title"] == "Example Feed"
    assert feed["unread"] == 2


def test_items_scopes(client, seeded):
    all_items = client.get("/api/items?scope=all").get_json()["items"]
    assert len(all_items) == 3
    # Newest first by published date.
    assert all_items[0]["title"] == "Third read item"

    unread = client.get("/api/items?scope=unread").get_json()["items"]
    assert len(unread) == 2

    starred = client.get("/api/items?scope=starred").get_json()["items"]
    assert len(starred) == 1
    assert starred[0]["starred"] == 1

    later = client.get("/api/items?scope=read_later").get_json()["items"]
    assert len(later) == 1
    assert later[0]["read_later"] == 1

    feed = client.get(f"/api/items?scope=feed&id={seeded['fid']}").get_json()["items"]
    assert len(feed) == 3


def test_items_search(client, seeded):
    r = client.get("/api/items?scope=all&q=widgets").get_json()["items"]
    assert len(r) == 1
    assert "widgets" in r[0]["title"]


def test_items_flagged_uses_watchlist(client, seeded, app_mod):
    app_mod.set_setting("watchlist", "CVE-2021-1234")
    r = client.get("/api/items?scope=flagged").get_json()["items"]
    assert len(r) == 1
    assert "CVE-2021-1234" in r[0]["title"]


def test_mark_item_read_and_star(client, seeded):
    iid = seeded["items"][0]
    r = client.post(f"/api/items/{iid}/read", json={"read": True}).get_json()
    assert r == {"ok": True, "read": True}

    r = client.post(f"/api/items/{iid}/star", json={"starred": True}).get_json()
    assert r == {"ok": True, "starred": True}

    r = client.post(f"/api/items/{iid}/read-later", json={"read_later": True}).get_json()
    assert r == {"ok": True, "read_later": True}


def test_mark_read_scope_feed(client, seeded):
    client.post("/api/mark-read", json={"scope": "feed", "id": seeded["fid"]})
    unread = client.get("/api/items?scope=unread").get_json()["items"]
    assert unread == []


def test_mark_read_all(client, seeded):
    client.post("/api/mark-read", json={"scope": "all"})
    assert client.get("/api/tree").get_json()["totals"]["unread"] == 0


def test_settings_get_defaults(client):
    d = client.get("/api/settings").get_json()
    assert d["hide_shorts"] is True
    assert d["refresh_minutes"] in d["refresh_choices"]
    assert d["watchlist"] == []
    assert d["has_api_key"] is False


def test_settings_set_watchlist_dedupes_and_caps(client, app_mod):
    client.post("/api/settings", json={"watchlist": ["foo", "Foo", "bar", "foo"]})
    assert app_mod.get_watchlist() == ["foo", "bar"]


def test_settings_reject_bad_refresh(client):
    r = client.post("/api/settings", json={"refresh_minutes": 7})
    assert r.status_code == 400


def test_settings_api_key_is_write_only(client, app_mod):
    client.post("/api/settings", json={"anthropic_api_key": "sk-secret-123"})
    got = client.get("/api/settings").get_json()
    assert got["has_api_key"] is True
    assert "sk-secret-123" not in str(got)  # the key itself is never returned
    assert app_mod.get_api_key() == "sk-secret-123"
    # Blank values are ignored so unrelated saves don't wipe it.
    client.post("/api/settings", json={"anthropic_api_key": ""})
    assert app_mod.get_api_key() == "sk-secret-123"
    # Explicit clear removes it.
    client.post("/api/settings", json={"clear_api_key": True})
    assert app_mod.get_api_key() == ""


def test_categories_crud(client):
    r = client.post("/api/categories", json={"name": "Blogs"}).get_json()
    cid = r["id"]
    # A default 'General' subject is created so it's immediately usable.
    dup = client.post("/api/categories", json={"name": "Blogs"})
    assert dup.status_code == 400
    client.put(f"/api/categories/{cid}", json={"name": "Renamed"})
    assert client.delete(f"/api/categories/{cid}").get_json() == {"ok": True}


def test_subjects_crud(client, seeded):
    r = client.post("/api/subjects",
                    json={"name": "Threat Intel", "category_id": seeded["cid"]})
    assert r.status_code == 200
    sid = r.get_json()["id"]
    dup = client.post("/api/subjects",
                      json={"name": "Threat Intel", "category_id": seeded["cid"]})
    assert dup.status_code == 400
    client.put(f"/api/subjects/{sid}", json={"name": "Renamed Subject"})
    assert client.delete(f"/api/subjects/{sid}").get_json() == {"ok": True}


def test_subject_create_requires_category(client):
    r = client.post("/api/subjects", json={"name": "Orphan"})
    assert r.status_code == 400


def test_preview_missing_item(client):
    r = client.get("/api/items/99999/preview")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_preview_reads_url_from_db_only(client, seeded, app_mod, monkeypatch):
    # The preview route looks the URL up by item id (never a user-supplied URL),
    # then fetches it. Confirm it fetches exactly the stored URL.
    fetched = {}

    def fake_get(url, with_ctype=False):
        fetched["url"] = url
        page = b"<html><body><article><p>" + b"lots of real words " * 20 + \
               b"</p></article></body></html>"
        return (page, "text/html") if with_ctype else page

    monkeypatch.setattr(net, "http_get", fake_get)
    iid = seeded["items"][0]
    r = client.get(f"/api/items/{iid}/preview").get_json()
    assert r["ok"] is True
    assert fetched["url"] == "https://example.com/0"  # the stored item URL


def test_reset_cascades(client, seeded, app_mod):
    client.post("/api/reset")
    with app_mod.db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"] == 0
        # ON DELETE CASCADE clears subjects, feeds and items too.
        assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


def test_brief_get_without_key(client):
    d = client.get("/api/brief").get_json()
    assert d["has_api_key"] is False
    assert d["brief"] is None


def test_brief_post_without_key_errors(client):
    r = client.post("/api/brief")
    assert r.status_code == 400
    assert "API key" in r.get_json()["error"]


def test_feeds_health_shape(client, seeded):
    d = client.get("/api/feeds/health").get_json()
    assert d["summary"]["total"] == 1
    assert d["feeds"][0]["items"] == 3
    assert d["feeds"][0]["unread"] == 2
