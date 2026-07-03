"""AI Daily Brief — summarise the last 24h of feeds with the Claude API.

The ``anthropic`` package is imported lazily so the app still boots (with a
friendly error) if it isn't installed.
"""
import time
from datetime import datetime, timezone, timedelta

import config
from config import now_iso
import database

# Server-side cooldown so /api/brief can't be hammered into repeated (paid) API
# calls — the response is cached per day anyway, and regeneration is rarely needed
# more than once every few seconds. Guards against accidental double-clicks and
# CSRF/abuse alike.
_last_generation = 0.0


def _cooldown_remaining():
    """Seconds left before another brief generation is allowed (0 = allowed now)."""
    return max(0.0, config.BRIEF_COOLDOWN - (time.monotonic() - _last_generation))


def _note_generation():
    global _last_generation
    _last_generation = time.monotonic()


def _collect_brief_items():
    """Articles from the last 24h, newest first, capped — what the model summarises."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    hide = " AND i.is_short IS NOT 1" if database.hide_shorts_enabled() else ""
    with database.db() as conn:
        rows = conn.execute(
            "SELECT i.title, i.summary, f.title AS feed_title, c.name AS category "
            "FROM items i JOIN feeds f ON f.id = i.feed_id "
            "JOIN subjects s ON s.id = f.subject_id JOIN categories c ON c.id = s.category_id "
            "WHERE COALESCE(i.published, i.fetched_at) >= ?" + hide + " "
            "ORDER BY COALESCE(i.published, i.fetched_at) DESC LIMIT ?",
            (cutoff, config.BRIEF_MAX_ITEMS)).fetchall()
    return [dict(r) for r in rows]


def generate_brief():
    """Ask Claude to synthesise the day's feeds. Returns (brief_dict, error_str)."""
    key = database.get_api_key()
    if not key:
        return None, "No Anthropic API key set. Add one in Settings → AI Daily Brief."
    items = _collect_brief_items()
    if not items:
        return None, "No articles from the last 24 hours to summarise yet."
    wait = _cooldown_remaining()
    if wait > 0:
        return None, f"The brief was just generated — please wait {int(wait) + 1}s and try again."
    try:
        import anthropic
    except ImportError:
        return None, "The 'anthropic' package isn't installed — rebuild the container (it's in requirements.txt)."

    lines = []
    for it in items:
        summ = (it["summary"] or "")[:300]
        lines.append(f"- [{it['category']} / {it['feed_title']}] {it['title']}"
                     + (f" — {summ}" if summ else ""))
    watch = database.get_watchlist()
    watch_note = ("\nThe reader is especially interested in these topics: "
                  + ", ".join(watch) + ". Surface anything matching them prominently."
                  ) if watch else ""
    system = (
        "You are a cybersecurity news editor. You are given today's article headlines and "
        "summaries from the reader's own feeds. Write a concise daily brief in Markdown:\n"
        "- Open with a 1-2 sentence overview of what matters most today.\n"
        "- Then group items under headings by urgency: '## Actively exploited / urgent', "
        "'## Worth a look', '## Background'. Omit any group that would be empty.\n"
        "- Merge the same story reported by multiple sources into a single bullet.\n"
        "- One short line per item on why it matters; name the source(s).\n"
        "- Be specific and skimmable. Do not invent anything that isn't in the items."
        + watch_note)
    user = "Here are the articles from the last 24 hours:\n\n" + "\n".join(lines)

    _note_generation()   # start the cooldown now, before the (paid) call goes out
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=config.BRIEF_MODEL, max_tokens=4000,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}])
    except anthropic.AuthenticationError:
        return None, "Anthropic rejected the API key — check it in Settings."
    except anthropic.APIStatusError as e:                     # noqa: BLE001
        return None, f"Anthropic API error ({e.status_code}). Try again later."
    except Exception as e:                                    # noqa: BLE001
        return None, f"Couldn't generate the brief: {e}"

    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text").strip()
    if not text:
        return None, "The model returned an empty brief. Try again."

    day = datetime.now(timezone.utc).date().isoformat()
    with database.db() as conn:
        conn.execute(
            "INSERT INTO briefs (day, content, created_at, item_count) VALUES (?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET content=excluded.content, "
            "created_at=excluded.created_at, item_count=excluded.item_count",
            (day, text, now_iso(), len(items)))
    return {"content": text, "created_at": now_iso(), "item_count": len(items), "day": day}, None
