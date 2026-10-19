# Changes (security, payments, plans, token system)

## Deploy order
1. Back up the Supabase database (Dashboard > Database > Backups, or `pg_dump`).
2. Replace the project files with this package (keep your own `.env` / Render env vars).
   Delete these old templates if present: `forgot-password.html`, `reset-password.html`,
   `verify-email.html`, `verify-pending.html`, `voice-test.html` (unused).
3. Add the Render environment variables below, then deploy.
4. Open `/api/admin/ip-debug` while logged in as admin and follow "Guest IP".
5. Create the Render Cron Job for plan expiry (below).

New tables (`call_requests`, `plan_events`) and the new columns on `api_usage_requests`
are created automatically at startup. If your Supabase role cannot run DDL, run `migration.sql`.

## Environment variables
| Name | Required | Purpose |
|---|---|---|
| SECRET_KEY | yes | already set; keep the same value |
| SITE_URL | yes | e.g. `https://khayyam-ai.onrender.com`; base of links inside emails |
| CRON_SECRET | yes | long random string protecting `/internal/plans/sweep` |
| ADMIN_NOTIFY_EMAIL | optional | you get an email for every new call-us request |
| CLIENT_IP_HOPS | yes, set `2` | Render is behind Cloudflare; makes guest limits per real visitor IP (see Guest IP) |
| GUEST_USD_PER_MONTH | optional | per-guest dollar cap (default 0.10) |
| GUEST_GLOBAL_DAILY_USD | optional | ALL guests combined per day (default 3.0) |
| ROUTER_CANNED | optional | `false` turns off free local greeting replies |
| RATELIMIT_STORAGE_URI | optional | Redis URL for shared rate limits (default: per-process memory) |
| PYTHON_VERSION | if build fails | e.g. `3.13.5` (requirements are pinned for 3.11+) |

## Guest IP (important)
Guests are limited per visitor IP. Behind Render's proxy the app must read the real IP.
Open `/api/admin/ip-debug` as admin. If `resolved` is YOUR real IP, nothing to do.
If it shows a proxy/Cloudflare address, set `CLIENT_IP_HEADER` to the header carrying your IP
(`CF-Connecting-IP` or `True-Client-IP`, shown on that page), or set `CLIENT_IP_HOPS=2`.

Render sits behind Cloudflare, so the right value is `CLIENT_IP_HOPS=2` (X-Forwarded-For arrives as
`visitor, cloudflare`). Verified against a real `ip-debug` reading: with 2, `resolved` equals the visitor IP.

## Render Cron Job (plan expiry + 3-day reminders)
Type: Cron Job, schedule `0 6 * * *`, command:
`curl -fsS -X POST -H "X-Cron-Secret: $CRON_SECRET" "$SITE_URL/internal/plans/sweep"`
Without it, expiry still happens the moment an expired user makes any request, but inactive users
would not get their "plan ended" email and nobody gets reminders.

## What changed
Security: CSRF protection on every write (token added automatically by `static/main.js`); rate limits on
login/register/reset/chat/voice/tutor/call-us; real client IP for guests; AI replies sanitised with DOMPurify
(marked + DOMPurify are self-hosted in `static/vendor/`); user text escaped in admin/profile/pricing;
security headers; email links built from SITE_URL (no Host-header poisoning); bcrypt long-password crash fixed
(Dari passwords over ~36 characters would have failed on bcrypt 5); username charset validated; pinned
requirements; gunicorn 2 workers x 4 threads; DB connections validated before use; OpenAI/Groq timeouts
(45s/30s); `persona-chat` now requires login; `instance/` and `*.db` added to `.gitignore`.

Plans: `grant_plan()` (monthly, stackable: renewing the same plan adds 30 days per month to the current expiry;
switching plan starts a fresh period; chat and tutor are independent), automatic expiry with email, one reminder
email 3 days before expiry, audit log table `plan_events`, admin panel rebuilt for both plans with months and
expiry dates, `/api/me` + profile page show plans, expiry and usage.

Call-us: pricing page form (guests and users), `call_requests` table, admin tab (call, mark contacted,
"paid - activate plan" which grants the plan and emails the customer), optional email alert to you.

Token system: see `TOKEN_SYSTEM.md`.
