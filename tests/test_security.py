"""Tests for the Step-2 hardening: SSRF guard, XML defusing, brief cooldown,
CSRF same-origin check and optional Basic auth."""
import base64

import pytest

import net
import config
import content
import brief


# ── SSRF guard ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://[::1]/",
])
def test_guard_blocks_internal_addresses(url):
    with pytest.raises(net.BlockedURLError):
        net.guard_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/"])
def test_guard_blocks_non_http_schemes(url):
    with pytest.raises(net.BlockedURLError):
        net.guard_url(url)


def test_guard_allows_public_host(monkeypatch):
    # Pretend DNS resolves the host to a public address.
    monkeypatch.setattr(net.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))])
    net.guard_url("https://example.com/feed.xml")   # must not raise


def test_guard_blocks_public_host_that_resolves_internal(monkeypatch):
    # A hostname that resolves to a private IP (DNS rebinding style) is refused.
    monkeypatch.setattr(net.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(net.BlockedURLError):
        net.guard_url("https://sneaky.example/")


def test_guard_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "BLOCK_PRIVATE_IPS", False)
    net.guard_url("http://127.0.0.1/")   # allowed when explicitly turned off


def test_http_get_refuses_internal_without_network():
    # guard_url runs before any socket is opened, so this never touches the network.
    with pytest.raises(net.BlockedURLError):
        net.http_get("http://127.0.0.1:9/")


# ── XML defusing ──────────────────────────────────────────────────────────────
BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;">
]>
<rss><channel><item><title>&lol2;</title></item></channel></rss>"""


def test_parse_feed_rejects_entity_expansion(app_mod):
    with pytest.raises(ValueError):
        content.parse_feed(BILLION_LAUGHS)


def test_opml_import_rejects_entities(app_mod):
    with pytest.raises(ValueError):
        app_mod.import_opml(BILLION_LAUGHS)


def test_opml_import_endpoint_rejects_entity_bomb(client):
    import io
    r = client.post("/api/opml/import",
                    data={"file": (io.BytesIO(BILLION_LAUGHS), "x.opml")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


# ── Brief cooldown ────────────────────────────────────────────────────────────
def test_brief_cooldown_mechanism(monkeypatch):
    monkeypatch.setattr(config, "BRIEF_COOLDOWN", 30)
    brief._note_generation()
    assert brief._cooldown_remaining() > 0
    # Simulate the cooldown having elapsed.
    monkeypatch.setattr(brief, "_last_generation", brief.time.monotonic() - 1000)
    assert brief._cooldown_remaining() == 0


def test_generate_brief_blocked_during_cooldown(app_mod, seeded, monkeypatch):
    # A fresh item so _collect_brief_items() isn't empty.
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO items (feed_id, guid, title, url, published, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (seeded["fid"], "fresh", "Fresh news", "https://x/y",
             app_mod.now_iso(), app_mod.now_iso()))
    app_mod.set_setting("anthropic_api_key", "sk-test")
    monkeypatch.setattr(config, "BRIEF_COOLDOWN", 30)
    brief._note_generation()   # force the cooldown
    result, err = brief.generate_brief()
    assert result is None
    assert "wait" in err.lower()


# ── CSRF same-origin guard ────────────────────────────────────────────────────
def test_csrf_blocks_foreign_origin(client, seeded):
    r = client.post("/api/mark-read", json={"scope": "all"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_csrf_allows_same_origin(client, seeded):
    r = client.post("/api/mark-read", json={"scope": "all"},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 200


def test_csrf_allows_no_origin(client, seeded):
    # Non-browser clients (curl) send no Origin/Referer and are allowed.
    r = client.post("/api/mark-read", json={"scope": "all"})
    assert r.status_code == 200


# ── Optional Basic auth ───────────────────────────────────────────────────────
def test_auth_required_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "alice")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret")
    assert client.get("/api/tree").status_code == 401
    token = base64.b64encode(b"alice:s3cret").decode()
    ok = client.get("/api/tree", headers={"Authorization": f"Basic {token}"})
    assert ok.status_code == 200
    bad = client.get("/api/tree",
                     headers={"Authorization": "Basic " + base64.b64encode(b"alice:wrong").decode()})
    assert bad.status_code == 401


def test_auth_open_by_default(client):
    assert client.get("/api/tree").status_code == 200
