"""Test harness for CyberNewsHub.

Two things about ``app.py`` make it awkward to import under test, and this
conftest neutralises both *before* the import so the suite is hermetic and
offline:

1. At import time the module calls ``init_db()`` (fine — offline) **and** starts
   a background refresh thread that fetches every feed over the network. We stub
   ``threading.Thread`` with a no-op so nothing is ever fetched in the background,
   and any route that spawns a thread (refresh / opml-import / feed-create) simply
   does nothing extra — its return value doesn't depend on the thread finishing.
2. ``init_db()`` seeds the DB from ``seed-feeds.opml`` on first run. We point
   ``DB_PATH`` at a temp file and blank out ``SEED_OPML`` per test so every test
   starts from a clean, known schema and inserts its own fixtures.

These are characterization tests: they pin the app's CURRENT behaviour so the
refactor and security steps can't silently change it.
"""
import importlib
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _NoopThread:
    """Stand-in for threading.Thread whose start() does nothing."""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def join(self, *a, **k):
        pass


# Stub threading.Thread BEFORE importing app, so the module-level background
# refresh thread never starts. Kept stubbed for the whole session on purpose.
_real_thread = threading.Thread
threading.Thread = _NoopThread

# First import happens with DB_PATH pointing somewhere harmless; each test then
# repoints it. We import once and reuse the module object.
import os

os.environ.setdefault("DB_PATH", str(PROJECT_ROOT / "tests" / "_import_only.db"))
app_module = importlib.import_module("app")
import config  # noqa: E402  — imported after sys.path is set up


@pytest.fixture
def app_mod(tmp_path):
    """The imported app module (facade), wired to a fresh empty DB with no seed.

    DB_PATH / SEED_OPML live in ``config`` and are read at call time, so the
    reassignment here is seen by db()/init_db() across every module.
    """
    config.DB_PATH = tmp_path / "test.db"
    config.SEED_OPML = tmp_path / "nonexistent-seed.opml"  # skip auto-seed
    app_module.init_db()
    return app_module


@pytest.fixture
def client(app_mod):
    app_mod.app.config.update(TESTING=True)
    return app_mod.app.test_client()


@pytest.fixture
def seeded(app_mod):
    """A small, known dataset: 1 category > 1 subject > 1 feed > 3 items.

    Returns a dict of the ids so tests can target them.
    """
    with app_mod.db() as conn:
        cid = conn.execute(
            "INSERT INTO categories (name, sort_order) VALUES ('News', 1)"
        ).lastrowid
        sid = conn.execute(
            "INSERT INTO subjects (category_id, name, sort_order) VALUES (?, 'General', 1)",
            (cid,),
        ).lastrowid
        fid = conn.execute(
            "INSERT INTO feeds (subject_id, title, url, site_url, type) "
            "VALUES (?, 'Example Feed', 'https://example.com/feed.xml', "
            "'https://example.com', 'rss')",
            (sid,),
        ).lastrowid
        items = []
        for n, (title, read, starred, later) in enumerate(
            [
                ("First CVE-2021-1234 alert", 0, 0, 0),
                ("Second story about widgets", 0, 1, 0),
                ("Third read item", 1, 0, 1),
            ]
        ):
            iid = conn.execute(
                "INSERT INTO items (feed_id, guid, title, url, summary, published, "
                "read, starred, read_later, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    f"guid-{n}",
                    title,
                    f"https://example.com/{n}",
                    f"Summary for item {n}",
                    f"2026-06-2{n}T00:00:00+00:00",
                    read,
                    starred,
                    later,
                    "2026-06-20T00:00:00+00:00",
                ),
            ).lastrowid
            items.append(iid)
    return {"cid": cid, "sid": sid, "fid": fid, "items": items}
