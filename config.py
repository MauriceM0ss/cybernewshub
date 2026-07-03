"""Runtime configuration and small shared primitives.

Values that get reassigned under test (DB_PATH, SEED_OPML) live here and must be
accessed as ``config.NAME`` by other modules — never ``from config import NAME`` —
so a reassignment is seen everywhere.
"""
import os
from pathlib import Path
from datetime import datetime, timezone

DB_PATH        = Path(os.environ.get("DB_PATH", "/data/feeds.db"))
SEED_OPML      = Path(__file__).parent / "seed-feeds.opml"

REFRESH_CHOICES         = (10, 15, 30, 60)                      # auto-refresh options (minutes)
DEFAULT_REFRESH_MINUTES = max(1, int(os.environ.get("REFRESH_MINUTES", "30")))
ITEM_RETENTION = int(os.environ.get("ITEM_RETENTION", "400"))   # max kept items per feed

# YouTube rate-limits its RSS endpoint per IP and returns 404 (not 429) when throttling.
# Fetch YouTube feeds one at a time with a delay, and don't flag a feed as broken until it
# has failed this many times in a row — a transient throttle clears long before that.
YT_FETCH_DELAY    = float(os.environ.get("YT_FETCH_DELAY", "1.5"))   # seconds between YouTube fetches
YT_FAIL_THRESHOLD = max(1, int(os.environ.get("YT_FAIL_THRESHOLD", "3")))

PAGE_SIZE      = 40
HTTP_TIMEOUT   = 20
MAX_BYTES      = 8 * 1024 * 1024                                # cap a single download
USER_AGENT     = "CyberNewsHub/1.0 (+https://github.com/MauriceM0ss)"

BRIEF_MODEL     = os.environ.get("BRIEF_MODEL", "claude-opus-4-8")   # AI daily brief
BRIEF_MAX_ITEMS = 120                                               # cap items sent to the model
BRIEF_COOLDOWN  = int(os.environ.get("BRIEF_COOLDOWN", "30"))       # min seconds between (re)generations


def _envflag(name, default="1"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# Security: block server-side fetches to private/loopback/link-local/reserved IPs
# (SSRF protection). On by default; set BLOCK_PRIVATE_IPS=0 only if you genuinely
# host feeds on a private network you trust.
BLOCK_PRIVATE_IPS = _envflag("BLOCK_PRIVATE_IPS", "1")

# Optional HTTP Basic auth. When both are set, every request must authenticate;
# when unset the app is open (unchanged default for localhost/self-hosted use).
AUTH_USER     = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")


class SourceError(Exception):
    """A source URL could not be resolved to a usable feed."""


def now_iso():
    return datetime.now(timezone.utc).isoformat()
