"""Characterization tests for feed parsing, date handling and source resolution."""
import net
import pytest


RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example Security Blog</title>
    <item>
      <title>Big breach reported</title>
      <link>https://example.com/breach</link>
      <description>&lt;p&gt;Some &lt;b&gt;HTML&lt;/b&gt; summary&lt;/p&gt;</description>
      <pubDate>Wed, 18 Jun 2025 12:00:00 GMT</pubDate>
      <guid>https://example.com/breach</guid>
    </item>
    <item>
      <title>Second story</title>
      <link>https://example.com/second</link>
    </item>
  </channel>
</rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <title>Atom entry one</title>
    <link rel="alternate" href="https://atom.example/one"/>
    <link rel="edit" href="https://atom.example/edit"/>
    <id>tag:atom.example,2025:1</id>
    <updated>2025-06-18T09:30:00Z</updated>
    <summary>Plain atom summary</summary>
  </entry>
</feed>"""


def test_parse_feed_rss(app_mod):
    items = app_mod.parse_feed(RSS)
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "Big breach reported"
    assert first["url"] == "https://example.com/breach"
    assert first["guid"] == "https://example.com/breach"
    # HTML in the description is stripped to plain text.
    assert first["summary"] == "Some HTML summary"
    assert first["published"] == "2025-06-18T12:00:00+00:00"


def test_parse_feed_atom_prefers_alternate_link(app_mod):
    items = app_mod.parse_feed(ATOM)
    assert len(items) == 1
    assert items[0]["title"] == "Atom entry one"
    # rel="alternate" wins over rel="edit".
    assert items[0]["url"] == "https://atom.example/one"
    assert items[0]["published"] == "2025-06-18T09:30:00+00:00"


def test_parse_feed_skips_empty_entries(app_mod):
    data = b"""<rss><channel>
      <item><title></title><link></link></item>
      <item><title>Keep me</title></item>
    </channel></rss>"""
    items = app_mod.parse_feed(data)
    assert [i["title"] for i in items] == ["Keep me"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Wed, 18 Jun 2025 12:00:00 GMT", "2025-06-18T12:00:00+00:00"),
        ("2025-06-18T09:30:00Z", "2025-06-18T09:30:00+00:00"),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_date(app_mod, raw, expected):
    assert app_mod.parse_date(raw) == expected


def test_parse_date_naive_assumed_utc(app_mod):
    assert app_mod.parse_date("2025-06-18T09:30:00") == "2025-06-18T09:30:00+00:00"


def test_strip_html(app_mod):
    assert app_mod.strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert app_mod.strip_html("<script>evil()</script>text") == "text"
    assert app_mod.strip_html("a &amp; b") == "a & b"
    assert app_mod.strip_html("") == ""


def test_feed_title(app_mod):
    assert app_mod._feed_title(RSS) == "Example Security Blog"
    assert app_mod._feed_title(ATOM) == "Atom Feed"
    assert app_mod._feed_title(b"not xml") == ""


def test_discover_feed(app_mod):
    html = b"""<html><head>
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
    </head></html>"""
    assert app_mod._discover_feed(html, "https://site.example/") == \
        "https://site.example/feed.xml"


def test_discover_feed_none_when_absent(app_mod):
    assert app_mod._discover_feed(b"<html></html>", "https://x/") is None


def test_favicon_for(app_mod):
    assert app_mod.favicon_for("https://example.com", "") == \
        "https://icons.duckduckgo.com/ip3/example.com.ico"
    # Falls back to the feed URL host when no site URL.
    assert app_mod.favicon_for("", "https://feeds.example.org/x") == \
        "https://icons.duckduckgo.com/ip3/feeds.example.org.ico"
    assert app_mod.favicon_for("", "") == ""


@pytest.mark.parametrize(
    "guid,url,expected",
    [
        ("yt:video:ABC123xyz", "", "ABC123xyz"),
        ("", "https://youtu.be/DEF456uvw", "DEF456uvw"),
        ("", "https://www.youtube.com/watch?v=GHI789rst", "GHI789rst"),
        ("", "https://www.youtube.com/shorts/JKL012mno", "JKL012mno"),
        ("", "https://example.com/no-id", None),
    ],
)
def test_youtube_video_id(app_mod, guid, url, expected):
    assert app_mod._youtube_video_id(guid, url) == expected


def test_youtube_feed_channel_url(app_mod):
    feed, title = app_mod._youtube_feed(
        "https://www.youtube.com/channel/UCabcdef123456")
    assert feed == \
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdef123456"


def test_youtube_feed_playlist_url(app_mod):
    feed, _ = app_mod._youtube_feed(
        "https://www.youtube.com/playlist?list=PL123456")
    assert feed == \
        "https://www.youtube.com/feeds/videos.xml?playlist_id=PL123456"


def test_youtube_feed_handle_uses_external_id(app_mod, monkeypatch):
    # For an @handle, the resolver fetches the page and prefers externalId over a
    # bare channelId (which could be a featured/cross-linked channel).
    page = (b'<html><meta property="og:title" content="Cool Channel">'
            b'"channelId":"UCfeatured000000"'
            b'"externalId":"UCown1234567890"</html>')
    monkeypatch.setattr(net, "http_get", lambda url: page)
    feed, title = app_mod._youtube_feed("https://www.youtube.com/@coolchannel")
    assert feed == \
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCown1234567890"
    assert title == "Cool Channel"


def test_resolve_source_rss(app_mod, monkeypatch):
    monkeypatch.setattr(net, "http_get",
                        lambda url, with_ctype=False: (RSS, "application/rss+xml"))
    info = app_mod.resolve_source("example.com/feed.xml")
    assert info["type"] == "rss"
    assert info["title"] == "Example Security Blog"
    # A bare host is normalised to https://.
    assert info["feed_url"].startswith("https://")


def test_resolve_source_discovers_from_html(app_mod, monkeypatch):
    page = (b"<html><head><link rel='alternate' "
            b"type='application/rss+xml' href='/feed.xml'></head></html>")

    def fake_get(url, with_ctype=False):
        if url.endswith("/feed.xml"):
            return RSS if not with_ctype else (RSS, "application/rss+xml")
        return (page, "text/html") if with_ctype else page

    monkeypatch.setattr(net, "http_get", fake_get)
    info = app_mod.resolve_source("https://site.example")
    assert info["type"] == "rss"
    assert info["feed_url"] == "https://site.example/feed.xml"


def test_resolve_source_no_feed_raises(app_mod, monkeypatch):
    monkeypatch.setattr(net, "http_get",
                        lambda url, with_ctype=False: (b"<html></html>", "text/html"))
    with pytest.raises(app_mod.SourceError):
        app_mod.resolve_source("https://site.example")


def test_watchlist_sql_filter_escapes_wildcards(app_mod):
    app_mod.set_setting("watchlist", "50%_off\nransomware")
    clause, params = app_mod.watchlist_sql_filter()
    assert "ESCAPE" in clause
    # % and _ in a term are escaped so they match literally, not as wildcards.
    assert params[0] == "%50\\%\\_off%"


def test_watchlist_sql_filter_empty_matches_nothing(app_mod):
    app_mod.set_setting("watchlist", "")
    clause, params = app_mod.watchlist_sql_filter()
    assert clause == "0"
    assert params == []
