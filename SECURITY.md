# CyberNewsHub — Security Notes

A self-hosted news aggregator (Flask + SQLite, single container). This documents
the threat model, the findings from the hardening review, and how to run it
safely. It reflects the state after the hardening pass in the module-split
codebase (`config.py`, `net.py`, `sources.py`, …).

## Threat model

**What it is.** A personal, single-user web app you run for yourself. It fetches
RSS/Atom feeds, YouTube channels and web pages you subscribe to, stores articles
in SQLite, and can call the Anthropic API to summarise the day.

**Assets.** Your feed list and reading history; your Anthropic API key; the
availability of the host it runs on.

**Trust boundaries.**
1. **The browser → app HTTP API.** No accounts; whoever can reach the port can use
   the app. Intended deployment is `localhost` or a trusted LAN, optionally behind
   an authenticating reverse proxy.
2. **The app → the internet.** The server fetches attacker-influenced content:
   any feed you subscribe to, any site you paste, and the pages behind article
   links. Malicious feed/page content is the main *remote* threat.

**Primary adversaries.** (a) A malicious website you visit that scripts requests
to your local app (CSRF / SSRF pivot). (b) A hostile or compromised feed/website
serving malicious XML or HTML. (c) Anyone on the network if the port is exposed.

## Findings & status

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | **SSRF** — `/api/discover` and `/api/feeds` fetch user-supplied URLs server-side; without a guard they could reach `127.0.0.1`, `169.254.169.254` (cloud metadata), or LAN admin panels | High | **Fixed** |
| 2 | **XML entity-expansion / XXE** — feed XML and uploaded OPML were parsed with stdlib `ElementTree`, which is vulnerable to "billion laughs" and external entities | High | **Fixed** |
| 3 | **CSRF** — no protection on state-changing endpoints; with no auth, a page you visit could drive your local app | Medium | **Fixed** |
| 4 | **No authentication** — any client that reaches the port has full control | Medium | **Mitigated** (optional Basic auth added; off by default) |
| 5 | **AI brief spend** — `POST /api/brief` triggers a paid API call with no throttle | Medium | **Fixed** |
| 6 | **Reader-view XSS** — the preview renders extracted HTML via `innerHTML` | Medium | **Reviewed — safe** |
| 7 | **API key stored in plaintext** in SQLite | Low | **Accepted** |
| 8 | **Favicon hostnames leak** to DuckDuckGo's CDN | Low | **Accepted** |
| 9 | **No transport encryption** | Low | **Accepted** (deploy behind HTTPS) |

### Details

**1 — SSRF (fixed).** All outbound fetches funnel through `net.http_get`, which
now calls `net.guard_url()` first: it allows only `http`/`https` and resolves the
host, refusing any result in a private, loopback, link-local, reserved, multicast
or unspecified range (IPv4, IPv6, and IPv4-mapped IPv6). Controlled by
`BLOCK_PRIVATE_IPS` (default on). *Residual risk:* this is resolve-and-check, so a
determined **DNS-rebinding** attacker could change the record between the check and
the connection. Full protection needs pinning the checked IP into the connection;
accepted as low risk for a self-hosted personal app. The YouTube Shorts probe uses
a fixed `youtube.com` URL template (no user-controlled host).

**2 — XML defusing (fixed).** `content.parse_feed` / `content._feed_title` and
`opml.import_opml` now parse with `defusedxml` (`forbid_entities` + `forbid_external`
defaults), which rejects entity declarations and external references. A malicious
feed raises an error that `refresh_feed` records as a normal feed failure; a
malicious OPML upload returns HTTP 400.

**3 — CSRF (fixed).** A `before_request` hook rejects `POST/PUT/PATCH/DELETE` whose
`Origin` (or `Referer`) is a different site. Browsers always send one of these on a
cross-site request, so this blocks the "malicious site scripts your localhost app"
vector; non-browser clients (curl) send neither and are allowed. This complements —
does not replace — running with auth if the port is exposed.

**4 — Optional Basic auth (mitigated).** Set `AUTH_USER` and `AUTH_PASSWORD` to
require HTTP Basic auth on every request (constant-time compared). Left **off by
default** so the localhost experience is unchanged. For anything beyond localhost,
enable it *and* put TLS in front (Basic auth is base64, not encryption).

**5 — Brief cooldown (fixed).** `brief.generate_brief` enforces a server-side
cooldown (`BRIEF_COOLDOWN`, default 30s) before another generation, and the daily
brief is cached per UTC day. This bounds accidental double-clicks and abuse. Prompt
and response bodies are **not** logged.

**6 — Reader view (reviewed).** `readability.extract_readable` builds a tree, then
re-emits a **whitelist** of tags with all attributes stripped except `a[href]` and
`img[src|alt]`; URLs pass through `_safe_url` (http/https only, so `javascript:`
and `data:` are dropped); text and attribute values are HTML-escaped. Behaviour is
pinned by `tests/test_readability.py`. No change needed.

**7 — API key at rest (accepted).** The key lives in the `settings` table on the
Docker data volume, outside the repo (`.gitignore` covers `data/` and `*.db`).
`GET /api/settings` never returns it (only a `has_api_key` boolean). Encrypting it
at rest would be theatre: the host owner who could read the DB can also read the
key from the environment or process. Prefer supplying it via the `ANTHROPIC_API_KEY`
env var, which takes precedence and is never written to the DB.

**8 — Favicon leak (accepted).** The sidebar loads feed icons from
`icons.duckduckgo.com`, revealing subscribed-feed hostnames to a third party. Low
impact for a personal tool. A future option is a small server-side favicon proxy
that fetches and caches icons locally.

## Operational recommendations

- **Bind to localhost.** Publish the port as `127.0.0.1:8030:8080` in Compose if
  you only use it on the host, so it isn't reachable from the LAN.
- **Beyond localhost:** set `AUTH_USER`/`AUTH_PASSWORD` **and** terminate TLS at a
  reverse proxy (Caddy/nginx). HTTPS is also required for the PWA install and
  desktop-notification features (secure-context only).
- **Keep `BLOCK_PRIVATE_IPS=1`** unless you deliberately host a feed on a trusted
  private address.
- **Back up** the `cybernewsreader_ctnhub-data` volume (see README) — it holds the
  DB and the saved API key.
- **Updating:** rebuild with `docker compose up -d --build` to pick up dependency
  and base-image security fixes.

## Reporting

This is a personal project; open an issue on the repository for anything you find.
