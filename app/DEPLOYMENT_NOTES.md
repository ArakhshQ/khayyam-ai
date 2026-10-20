# Khayyam AI deployment notes

Set environment variables in Render (never commit them):
- `SECRET_KEY`: long random secret, stable across restarts. **Keep the existing session secret if migrating**; changing it signs users out and invalidates outstanding reset links.
- `OPENAI_API_KEY`: transcription and speech API.
- `SMTP_HOST`: your existing Brevo SMTP hostname.
- `SMTP_PORT`: `587` (STARTTLS).
- `SMTP_USER`: your existing Brevo SMTP login.
- `SMTP_PASSWORD`: Brevo SMTP key (not your regular account password).
- `SMTP_FROM`: your verified sender email address.
- `DATABASE_URL`: existing production database URL; keep unchanged to preserve users.
- `COOKIE_SECURE=true`: for production HTTPS (set false for local HTTP development).
- Optional `TTS_VOICE`, `TTS_MODEL`, `STT_MODEL`.

Brevo SMTP uses STARTTLS on port 587; check hosting provider's outbound SMTP policy. Verification emails are best effort at signup; accounts can still sign in if Brevo isn't configured. Resend via `POST /api/resend-verification` when signed in. Existing accounts remain accessible. Database startup adds `users.email_verified` if missing. Back up your production database before deploying.

The voice microphone works as a two-click control (start/stop). Transcription inserts text for review; sending is a separate action. Voice quality and Dari accent are model-dependent and must be tested with real speakers. Do not claim production readiness until SMTP, TTS, STT, and both pages are verified on the deployed site.

Security follow-ups before public launch: CSRF protection for session-authenticated write endpoints, shared-store rate limiting for login and email endpoints, and dedicated schema migration tooling (Alembic). Current in-process email throttle is only best effort across workers.
