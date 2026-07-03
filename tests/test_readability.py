"""Characterization tests for the reader-view extractor + HTML sanitizer.

This output is dropped into the DOM with innerHTML, so the sanitiser is a
security-critical surface. These tests pin its CURRENT behaviour before the
hardening pass touches anything nearby.
"""

ARTICLE_PAGE = """
<html><head><title>Ignore me</title><script>alert(1)</script></head>
<body>
  <nav><a href="/a">Home</a><a href="/b">About</a><a href="/c">More</a></nav>
  <article>
    <h1>The Headline</h1>
    <p>This is the first substantial paragraph of the article body, long enough
       to clear the paragraph-score threshold used by the extractor here.</p>
    <p>A second paragraph continues the story with even more useful prose so the
       article container clearly beats the navigation block on score.</p>
    <img src="/images/pic.png" alt="a picture">
  </article>
  <footer><a href="/x">footer link</a></footer>
</body></html>
"""


def test_extract_picks_article_body(app_mod):
    out = app_mod.extract_readable(ARTICLE_PAGE, "https://news.example/post")
    assert "first substantial paragraph" in out
    assert "<h1>The Headline</h1>" in out


def test_extract_drops_scripts_and_nav(app_mod):
    out = app_mod.extract_readable(ARTICLE_PAGE, "https://news.example/post")
    assert "alert(1)" not in out
    assert "<script" not in out
    # Nav/footer links are outside the chosen article container.
    assert "About" not in out
    assert "footer link" not in out


def test_extract_resolves_relative_urls(app_mod):
    out = app_mod.extract_readable(ARTICLE_PAGE, "https://news.example/post")
    assert 'src="https://news.example/images/pic.png"' in out
    assert 'alt="a picture"' in out
    assert 'loading="lazy"' in out


def test_safe_url_blocks_javascript_scheme(app_mod):
    assert app_mod._safe_url("javascript:alert(1)", "https://x/") is None
    assert app_mod._safe_url("data:text/html,evil", "https://x/") is None
    assert app_mod._safe_url("/ok", "https://x/") == "https://x/ok"
    assert app_mod._safe_url("https://y/z", "https://x/") == "https://y/z"


def test_serialize_strips_javascript_anchor(app_mod):
    page = """
    <article>
      <p>Body paragraph one with plenty of words to pass the score threshold set
         by the extractor, padding padding padding padding padding padding.</p>
      <p>Body paragraph two, again with enough real prose to be selected as the
         main article container over anything else on this page at all.</p>
      <a href="javascript:steal()">click me</a>
      <a href="https://safe.example/ok">safe link</a>
    </article>"""
    out = app_mod.extract_readable(page, "https://news.example/")
    assert "javascript:" not in out
    # The javascript: anchor is unwrapped to its text, the safe one is kept.
    assert "click me" in out
    assert 'href="https://safe.example/ok"' in out
    assert 'rel="noopener nofollow"' in out


def test_serialize_strips_arbitrary_attributes(app_mod):
    page = """
    <article>
      <p onclick="evil()" class="danger" style="x">Enough prose here to select
         this container as the article body, with more and more words added on so
         the paragraph score comfortably clears the required threshold value.</p>
      <p>Second paragraph of genuine article text to reinforce the selection of
         this block as the winning readable container over the rest of page.</p>
    </article>"""
    out = app_mod.extract_readable(page, "https://news.example/")
    assert "onclick" not in out
    assert "class=" not in out
    assert "style=" not in out
    assert "<p>" in out


def test_void_element_does_not_swallow_page(app_mod):
    # A dropped void element (meta/link) must not put the parser into
    # skip-until-close mode, or it would eat the rest of the document.
    page = """
    <html><body>
      <meta charset="utf-8">
      <article>
        <p>The real article text comes after a meta tag and must survive intact,
           with lots of words so the container is chosen as readable content.</p>
        <p>More article text in a second paragraph to ensure this block wins the
           scoring and is returned by the extractor without being swallowed.</p>
      </article>
    </body></html>"""
    out = app_mod.extract_readable(page, "https://news.example/")
    assert "real article text" in out


def test_extract_returns_empty_for_junk(app_mod):
    # A page with no real prose yields '' so the caller shows "open in new tab".
    out = app_mod.extract_readable("<html><body><nav>x</nav></body></html>",
                                   "https://x/")
    assert out == ""
