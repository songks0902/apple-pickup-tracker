# Apple Store Pickup Availability Tracker

A small, stdlib-only Python service that polls Apple's public pickup-availability
API on a server (browsers never call Apple directly), renders a compact
one-screen status table, and sends flip-only alerts via ntfy and/or email.

> Sanitized backup copy: all real credentials, email addresses, domains, IPs,
> ZIP codes, ntfy topics, and SSH details have been replaced with placeholders.
> Real runtime data (`data/`), `.env`, cache/alert-state files, and preview
> screenshots are intentionally omitted from this repository.

## What it does

1. Fetches product metadata from Apple's `product-locator-meta` endpoint
   (cached for 24 hours), which lists every part number for a product family.
2. Polls Apple's `pickup-message` retail endpoint — one merged request for all
   parts near a ZIP code — at most every 120 seconds (hard floor, plus
   0–30 s positive jitter; single-flight, so page views never add Apple load).
3. Classifies each (store × part) into four states:
   - `available` — pickupDisplay `available` ("Available Today/Tomorrow")
   - `prelaunch` — `default` ("Check back for availability on …"); not yet on
     sale, **not** the same as out of stock
   - `unavailable` — `unavailable` ("Unavailable for Pickup")
   - `unknown` — missing/unexpected fields; never shown or treated as no stock,
     and never participates in alert transitions (last known state is kept)
4. Serves a password-protected page: one row per store, one column per part,
   target part highlighted, available cells link to Apple's buy page. The page
   auto-refreshes every 60 s from local cache only. If data is older than
   `STALE_SECONDS` (default 300 s) or the service is cooling down, cells render
   as "unknown" with a banner saying the data is stale — never as sold out.
5. Alerts **only on binary flips** per (store, part)
   (`pickupDisplay == 'available'` false→true = "available", true→false =
   "sold out"). First run builds a silent baseline; state is persisted to
   `data/alert_state.json` so restarts don't re-alert. Before an "available"
   alert is sent, availability is instantly re-checked with one more merged
   request; unconfirmed alerts are held. Sold-out alerts are not re-checked.

## Risk controls (Apple rate limiting)

- HTTP 403 / 429 / **541** (and network errors / non-JSON) count as failures.
- Consecutive failures back off on a ladder: **5 → 10 → 20 → 30 minutes**
  (capped). During cooldown no Apple requests are made; at expiry exactly one
  probe is sent, and success immediately restores the 120 s cadence.
- `/api/status` exposes `backoff_until` / `next_probe_at` / `last_http_status` /
  `consecutive_failures` / `last_success_at` / `data_age_seconds`.

## Alerts

- **ntfy / webhook**: set `WEBHOOK_URL` (e.g. `https://ntfy.sh/your-topic` —
  use a private, unguessable topic). ntfy URLs get a plain-text alert with
  title; other URLs receive the alert as JSON. Failures are independent of
  email.
- **Email**: set `ALERT_EMAIL` plus `SMTP_HOST` / `SMTP_USER` /
  `SMTP_PASSWORD` / `SMTP_FROM` (e.g. a app password, or another SMTP
  provider). If any SMTP value is empty, email is skipped with a log line.
- Alert bodies are minimal on purpose: spec, store name, pickup quote, buy
  link, tracker link. No store address/phone/hours are included.

## Configuration (`.env`)

Copy `.env.example` to `.env` and fill in your own values. Never commit `.env`.

| Variable | Meaning |
|---|---|
| `AUTH_USER` / `AUTH_PASSWORD` | HTTP Basic Auth for the page/API (use a strong random password) |
| `TRACK_FAMILY` | Apple product family slug, e.g. `iphoneduo` |
| `TRACK_ZIP` | US ZIP code to search around, e.g. `90210` (example) |
| `TARGET_PART` | Highlighted part number, e.g. `MK1H4LL/A` (public product info) |
| `PARTS` | Optional comma-separated part override; empty = all parts from meta |
| `POLL_SECONDS` | Poll interval; values below 120 are clamped to 120 |
| `WEBHOOK_URL` | ntfy topic URL or generic webhook |
| `PUBLIC_URL` | Public URL of this tracker, included in alerts |
| `ALERT_EMAIL`, `SMTP_*` | Optional email alerts |
| `AUTH_FAIL_LIMIT/WINDOW/BLOCK_SECONDS` | Brute-force guard tuning (default 10 / 600 s / 900 s) |

## Run locally (no Docker)

```bash
cp .env.example .env   # edit AUTH_PASSWORD etc.
set -a; . ./.env; set +a
python3 app.py         # http://localhost:8080
```

## Run with Docker

```bash
cp .env.example .env   # set a strong AUTH_PASSWORD
docker compose up -d --build
curl -u your-user:your-password http://localhost:8080/api/status
```

## Deploy on a VPS (generic)

```bash
# from your machine (replace host/key with your own)
scp -i ~/.ssh/your_key -r ./apple-tracker user@203.0.113.10:~/
ssh -i ~/.ssh/your_key user@203.0.113.10
cd ~/apple-tracker && cp .env.example .env   # fill in real values on the server only
docker compose -f docker-compose.vps.yml up -d --build
```

In the VPS compose file the web service is not published publicly; only Caddy
(80/443) is exposed. Point `tracker.example.com` (your own domain) at the
server, set the site address in `Caddyfile`, and Caddy obtains a Let's Encrypt
certificate automatically. If the domain is behind Cloudflare, use Cloudflare
SSL/TLS mode **Full (Strict)** (free) so both legs are encrypted, and
optionally enable Bot Fight Mode under Security → Bots.

Useful commands on the server:

```bash
docker compose -f docker-compose.vps.yml ps
docker compose -f docker-compose.vps.yml logs -f
docker compose -f docker-compose.vps.yml up -d --build   # after updating code
docker compose -f docker-compose.vps.yml restart
```

## Security notes

- Basic Auth uses constant-time comparison; `/healthz` is the only public
  endpoint. Responses send `Cache-Control: no-store` and `X-Robots-Tag: noindex`.
- Brute-force guard: after `AUTH_FAIL_LIMIT` failed logins from one IP within
  `AUTH_FAIL_WINDOW` seconds, that IP gets 429 + `Retry-After` for
  `AUTH_BLOCK_SECONDS` seconds (even correct passwords are refused during the
  block; a successful login clears the counter; in-memory only, resets on
  restart).
- The real client IP is trusted from forwarding headers only when the direct
  peer is a private/loopback proxy (e.g. Caddy on the Docker network); behind
  Cloudflare, Caddy overwrites forwarding headers with `CF-Connecting-IP`, so
  IPs can't be spoofed to evade the block.
- Never expose port 8080 directly to the internet without auth + HTTPS. Keep
  `.env`, ntfy topics, and app passwords out of git, chats, and screenshots.

## Files

- `app.py` — single-file stdlib service: poller, classifier, alerts, web UI
- `test_state_classifier.py` — classifier + synthetic meta/pickup fixtures
- `test_transitions.py` — flip logic + dry-run ntfy/email capture tests
- `test_risk_controls.py` — 541/backoff ladder, unknown-state, recheck, stale UI
- `test_render.py` — HTML structure / mobile rendering checks
- `test_auth_guard.py` — integration test for the brute-force blocker
- `Dockerfile`, `docker-compose.yml` (local), `docker-compose.vps.yml` (VPS)
- `Caddyfile` — reverse proxy + HTTPS (example domain)
- `.env.example` — placeholder configuration (no real secrets)

## Omitted from this sanitized copy

- `.env` (real credentials) — replaced by `.env.example`
- `data/` — runtime cache, metadata, and `alert_state.json`
- `preview-*.png` screenshots — omitted; they showed the configured ZIP/target
- Real domains, IPs, email addresses, and topics — replaced with
  `tracker.example.com`, `203.0.113.10`, `alert@example.com`,
  `https://ntfy.sh/your-topic`, and example ZIP `90210`

## Tests

```bash
python3 test_state_classifier.py
python3 test_transitions.py
python3 test_risk_controls.py
python3 test_render.py
python3 test_auth_guard.py   # spins up the app on a local test port
```

All tests are offline (network and SMTP are stubbed) and use synthetic data.
