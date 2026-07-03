"""SQLite connection, schema/migrations and the key/value settings helpers."""
import os
import sqlite3

import config


def db():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS subjects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                name        TEXT    NOT NULL,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                UNIQUE(category_id, name)
            );
            CREATE TABLE IF NOT EXISTS feeds (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id   INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                title        TEXT    NOT NULL DEFAULT '',
                url          TEXT    NOT NULL UNIQUE,        -- the feed (xml) url
                site_url     TEXT    NOT NULL DEFAULT '',
                type         TEXT    NOT NULL DEFAULT 'rss', -- rss | youtube
                sort_order   INTEGER NOT NULL DEFAULT 0,
                last_fetched TEXT    NOT NULL DEFAULT '',
                last_error   TEXT    NOT NULL DEFAULT '',
                fail_count   INTEGER NOT NULL DEFAULT 0   -- consecutive failed fetches
            );
            CREATE TABLE IF NOT EXISTS items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id    INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
                guid       TEXT    NOT NULL,
                title      TEXT    NOT NULL DEFAULT '',
                url        TEXT    NOT NULL DEFAULT '',
                summary    TEXT    NOT NULL DEFAULT '',
                author     TEXT    NOT NULL DEFAULT '',
                published  TEXT,                              -- ISO UTC, or NULL
                thumbnail  TEXT    NOT NULL DEFAULT '',
                read       INTEGER NOT NULL DEFAULT 0,
                starred    INTEGER NOT NULL DEFAULT 0,
                fetched_at TEXT    NOT NULL DEFAULT '',
                UNIQUE(feed_id, guid)
            );
            CREATE INDEX IF NOT EXISTS idx_items_feed    ON items(feed_id);
            CREATE INDEX IF NOT EXISTS idx_items_sort    ON items(published, fetched_at);
            CREATE INDEX IF NOT EXISTS idx_items_unread  ON items(read);
            """
        )
        # ── migrations ────────────────────────────────────────────────────────
        icols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        if "is_short" not in icols:
            # NULL = not yet classified; 1 = YouTube Short; 0 = regular video / article.
            conn.execute("ALTER TABLE items ADD COLUMN is_short INTEGER")
        if "read_later" not in icols:
            # 1 = bookmarked to read soon (kept past retention, like starred).
            conn.execute("ALTER TABLE items ADD COLUMN read_later INTEGER NOT NULL DEFAULT 0")
        fcols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
        if "fail_count" not in fcols:
            conn.execute("ALTER TABLE feeds ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS briefs ("
            "  day        TEXT    PRIMARY KEY,"   # UTC date, e.g. 2026-06-18
            "  content    TEXT    NOT NULL,"
            "  created_at TEXT    NOT NULL,"
            "  item_count INTEGER NOT NULL DEFAULT 0)")

        # First run: seed the taxonomy + feeds from the bundled OPML.
        empty = conn.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"] == 0
    if empty and config.SEED_OPML.exists():
        import opml   # local import: opml imports this module
        try:
            opml.import_opml(config.SEED_OPML.read_bytes())
        except Exception as e:                                # noqa: BLE001 — seed is best-effort
            import logging
            logging.getLogger(__name__).warning("Seed import failed: %s", e)


# ── Settings (simple key/value) ───────────────────────────────────────────────
def get_setting(key, default=None):
    with db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def hide_shorts_enabled():
    return get_setting("hide_shorts", "1") == "1"


def get_refresh_minutes():
    """How often the background loop auto-pulls new articles, in minutes."""
    try:
        m = int(get_setting("refresh_minutes", config.DEFAULT_REFRESH_MINUTES))
    except (TypeError, ValueError):
        return config.DEFAULT_REFRESH_MINUTES
    return m if m in config.REFRESH_CHOICES else config.DEFAULT_REFRESH_MINUTES


def get_watchlist():
    """Terms the user wants highlighted in titles/summaries (one per line)."""
    raw = get_setting("watchlist", "")
    return [t.strip() for t in raw.splitlines() if t.strip()]


def watchlist_sql_filter():
    """(clause, params) selecting items (alias ``i``) whose title or summary
    contains any watchlist term — the same case-insensitive substring match the
    frontend highlights with. Returns ('0', []) when the watchlist is empty, so
    the "Flagged" view/count matches nothing rather than everything."""
    terms = get_watchlist()
    if not terms:
        return "0", []
    clauses, params = [], []
    for t in terms:
        like = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(i.title LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\')")
        params += [like, like]
    return "(" + " OR ".join(clauses) + ")", params


def api_key_from_env():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_api_key():
    """The Anthropic API key: env var wins, else the (write-only) saved setting."""
    return os.environ.get("ANTHROPIC_API_KEY") or get_setting("anthropic_api_key", "") or ""
