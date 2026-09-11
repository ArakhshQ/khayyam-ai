from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from database import db, User, Conversation, Message, Memory, UserTokenUsage, SiteConfig, TutorProgress, QuizResult, StudentBadge, GuestUsage, GuestGlobalUsage, AccountToken, CallRequest, TutorTopicChat
from functools import wraps
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
from auth import register_user, login_user_by_username
from datetime import datetime, timezone, timedelta
from sqlalchemy import update as sa_update
from flask_limiter import Limiter
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import json
import re
import base64
import hashlib
import secrets
import smtplib
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

load_dotenv()

app = Flask(__name__)
# Render terminates SSL at its edge and forwards to the app over plain
# HTTP, so without this, Flask thinks every request arrived over HTTP -
# breaking request.is_secure (used below by CSRF's referrer check) and any
# future code that checks the request scheme. x_for=0 because
# get_client_ip() below already parses X-Forwarded-For itself.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1)
# ADMIN_PASSWORD is an admin login credential, not a session-signing key -
# these are different secrets and shouldn't share a value. Set a real
# SECRET_KEY in your environment; the random fallback still works but will
# invalidate sessions on every restart, so set it explicitly in production.
app.secret_key = os.getenv("SECRET_KEY") or os.getenv("ADMIN_PASSWORD", "fallback-secret")
database_url = os.getenv("DATABASE_URL", "sqlite:///khayyam.db")
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

# ── SESSION COOKIE SECURITY ──
# RENDER is set to "true" automatically on Render, and unset locally - used
# here so Secure cookies (HTTPS-only) are on in production but don't
# silently break local dev over plain http.
IS_PRODUCTION = bool(os.getenv("RENDER"))
app.config['SESSION_COOKIE_SECURE']   = IS_PRODUCTION
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ── CSRF PROTECTION ──
# No time limit: these are long-lived chat/tutor sessions, and a token that
# silently expires mid-session (while the tab stays open) would just look
# like a random broken request to the user. The token is still tied to the
# session and invalidated on logout, so this isn't a meaningful weakening.
app.config['WTF_CSRF_TIME_LIMIT'] = None
csrf = CSRFProtect(app)

@app.errorhandler(CSRFError)
def csrf_error(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "csrf_invalid", "reply": "نشست شما منقضی شده. صفحه را دوباره بارگذاری کنید."}), 400
    return jsonify({"error": "csrf_invalid"}), 400

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

@login_manager.unauthorized_handler
def unauthorized():
    # API routes should get a JSON 401, not an HTML redirect to /login
    if request.path.startswith('/api/'):
        return jsonify({"error": "login_required"}), 401
    return redirect(url_for('login_page'))

# Paths a logged-in, not-yet-verified user may still reach. Everything else
# (chat, tutor, profile, pricing, the homepage, every other /api/ route)
# is blocked until they verify - this re-checks on every single request,
# not just once at registration, so closing the tab and coming back later
# still enforces it. Guests (current_user not authenticated at all) are
# completely unaffected - this only ever applies to a logged-in session.
VERIFY_EXEMPT_PATHS = {
    '/verify-pending', '/verify-email', '/logout',
    '/api/resend-verification', '/api/me',
}

@app.before_request
def enforce_email_verification():
    if not current_user.is_authenticated:
        return  # guests are never gated - they have their own quota system
    if current_user.is_admin:
        return  # never let this policy lock out the site's own admin account
    if current_user.is_verified:
        return
    if request.path.startswith('/static/'):
        return
    if request.path in VERIFY_EXEMPT_PATHS:
        return

    if request.path.startswith('/api/'):
        return jsonify({"error": "verify_required", "message": "لطفاً ابتدا ایمیل خود را تایید کنید"}), 403
    return redirect(url_for('verify_pending_page'))

@app.before_request
def check_plan_expiry():
    """
    Runs on every authenticated request. If either of a user's plans has
    quietly passed its expiry date since they were last active, this
    catches and corrects it before the request is handled - so a request
    that would otherwise be served against a stale/expired paid plan
    instead sees the corrected (free) plan straight away.
    """
    if current_user.is_authenticated:
        check_and_expire_plans(current_user)

def get_client_ip():
    """
    Render (like most PaaS hosts) puts the app behind a reverse proxy, so
    request.remote_addr is the proxy's IP, not the visitor's. The real
    client IP is the first entry in X-Forwarded-For.
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"

limiter = Limiter(
    app=app,
    key_func=get_client_ip,
    default_limits=[],
    storage_uri="memory://"
    # NOTE: in-memory storage only works correctly with a single web
    # process/dyno. If you scale Render to more than one instance, switch
    # this to a Redis storage_uri or the per-IP limits below become
    # per-instance instead of global.
)

@app.errorhandler(429)
def rate_limit_exceeded(e):
    if request.path.startswith('/api/'):
        return jsonify({
            "error": "too_many_requests",
            "reply": "لطفاً کمی آهسته‌تر پیام بفرست — تعداد درخواست‌ها زیاد است."
        }), 429
    return e

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
groq_client   = Groq(api_key=os.getenv("GROQ_API_KEY"))

KNOWLEDGE_FILE = "knowledge.json"
EXAMPLES_FILE  = "examples.json"

# ── GUEST QUOTA CONFIG ──
GUEST_DAILY_TOKEN_LIMIT = int(os.getenv("GUEST_DAILY_TOKEN_LIMIT", 5000))
# Sitewide ceiling across ALL guests combined, per day - the real defense
# against someone rotating IPs to dodge the per-IP limit above.
GUEST_GLOBAL_DAILY_CAP  = int(os.getenv("GUEST_GLOBAL_DAILY_CAP", 200000))
GUEST_MODEL             = "gpt-5.4-mini"
_IP_SALT                = os.getenv("SECRET_KEY") or app.secret_key

def hash_ip(ip):
    """Store a salted hash instead of the raw IP - enough to rate-limit by, not enough to be a PII log of visitor IPs."""
    return hashlib.sha256(f"{_IP_SALT}:{ip}".encode()).hexdigest()

def today_key():
    return datetime.utcnow().strftime("%Y-%m-%d")

def next_utc_midnight_ts():
    now      = datetime.utcnow()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.replace(tzinfo=timezone.utc).timestamp()

def _get_or_create_guest_row(model, **filters):
    """
    get_or_create that's safe against two concurrent first-ever requests
    both trying to INSERT the same row at once — a plain "query, then
    insert if missing" isn't atomic, so under load two threads can both
    see "missing" and both attempt the insert. Only one insert wins (the
    unique constraint rejects the other with an IntegrityError); on that
    error we just roll back and re-fetch the row the winner created.
    """
    row = model.query.filter_by(**filters).first()
    if row:
        return row
    try:
        row = model(tokens_used=0, **filters)
        db.session.add(row)
        db.session.commit()
        return row
    except Exception:
        db.session.rollback()
        return model.query.filter_by(**filters).first()

def reserve_guest_tokens(estimate):
    """
    Atomically reserves `estimate` tokens against the per-IP daily cap AND
    the sitewide daily cap, BEFORE we spend money calling the model.
    Returns (allowed, ip_hash, date_key).

    Uses conditional UPDATE...WHERE statements (not read-then-write) so two
    concurrent requests from the same guest can't both pass the check before
    either one commits - the DB evaluates the WHERE clause against the
    latest row value at update time, so this is race-safe under both
    SQLite and Postgres without needing explicit locks.
    """
    ip_hash  = hash_ip(get_client_ip())
    date_key = today_key()

    try:
        row  = _get_or_create_guest_row(GuestUsage, ip_hash=ip_hash, date_key=date_key)
        grow = _get_or_create_guest_row(GuestGlobalUsage, date_key=date_key)

        result_ip = db.session.execute(
            sa_update(GuestUsage)
              .where(GuestUsage.id == row.id,
                     GuestUsage.tokens_used + estimate <= GUEST_DAILY_TOKEN_LIMIT)
              .values(tokens_used=GuestUsage.tokens_used + estimate, updated_at=datetime.utcnow())
        )
        if result_ip.rowcount == 0:
            db.session.rollback()
            return False, ip_hash, date_key

        result_global = db.session.execute(
            sa_update(GuestGlobalUsage)
              .where(GuestGlobalUsage.id == grow.id,
                     GuestGlobalUsage.tokens_used + estimate <= GUEST_GLOBAL_DAILY_CAP)
              .values(tokens_used=GuestGlobalUsage.tokens_used + estimate, updated_at=datetime.utcnow())
        )
        if result_global.rowcount == 0:
            # global cap hit - undo the per-IP reservation we just made
            db.session.execute(
                sa_update(GuestUsage).where(GuestUsage.id == row.id)
                  .values(tokens_used=GuestUsage.tokens_used - estimate)
            )
            db.session.commit()
            return False, ip_hash, date_key

        db.session.commit()
        return True, ip_hash, date_key
    except Exception as e:
        print(f"reserve_guest_tokens error: {e}")
        db.session.rollback()
        # fail CLOSED: if the quota system itself breaks, block the guest
        # request rather than silently allow unlimited spend
        return False, None, None

def true_up_guest_tokens(ip_hash, date_key, estimate, actual):
    """Corrects the reservation once the API tells us the real token count."""
    if not ip_hash or actual is None:
        return
    delta = actual - estimate
    if delta == 0:
        return
    try:
        db.session.execute(
            sa_update(GuestUsage)
              .where(GuestUsage.ip_hash == ip_hash, GuestUsage.date_key == date_key)
              .values(tokens_used=GuestUsage.tokens_used + delta)
        )
        db.session.execute(
            sa_update(GuestGlobalUsage)
              .where(GuestGlobalUsage.date_key == date_key)
              .values(tokens_used=GuestGlobalUsage.tokens_used + delta)
        )
        db.session.commit()
    except Exception as e:
        print(f"true_up_guest_tokens error: {e}")
        db.session.rollback()

# ── EMAIL (verification + password reset) ──
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM") or SMTP_USER
SITE_URL  = os.getenv("SITE_URL", "http://127.0.0.1:5000")  # e.g. https://khayyam.ai in production

def send_email(to_email, subject, html_body):
    """
    Sends a transactional email over SMTP. Works with any SMTP provider
    (SendGrid, Mailgun, Postmark, Resend, Amazon SES, or even Gmail for
    testing) - just set SMTP_HOST/PORT/USER/PASSWORD/FROM as env vars.
    Never raises: a broken email config should not break registration or
    password reset, it should just log and let the caller continue.
    """
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print(f"send_email skipped (SMTP not configured) - would have sent '{subject}' to {to_email}")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = SMTP_FROM
        msg['To']      = to_email
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"send_email error sending to {to_email}: {e}")
        return False

def create_account_token(user_id, purpose, ttl_minutes=60):
    """
    Creates a one-time token for the given purpose ('verify_email' or
    'reset_password'), invalidating any earlier unused tokens of the same
    purpose for this user first so old links stop working once a new one
    is requested. Returns the RAW token - only this return value should
    ever be emailed; the database only ever sees its hash.
    """
    raw_token  = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    db.session.execute(
        sa_update(AccountToken)
          .where(AccountToken.user_id == user_id, AccountToken.purpose == purpose,
                 AccountToken.used_at.is_(None))
          .values(used_at=datetime.utcnow())
    )
    db.session.add(AccountToken(
        user_id=user_id, token_hash=token_hash, purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes)
    ))
    db.session.commit()
    return raw_token

def consume_account_token(raw_token, purpose):
    """
    Validates and burns a token in one step. Returns the user_id on
    success, or None if the token is missing, wrong purpose, expired, or
    already used. A token can only ever be consumed once.
    """
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = AccountToken.query.filter_by(token_hash=token_hash, purpose=purpose).first()
    if not row or row.used_at is not None or row.expires_at < datetime.utcnow():
        return None
    row.used_at = datetime.utcnow()
    db.session.commit()
    return row.user_id

def build_email_html(heading, body_lines, button_text, button_link, footer_text):
    """
    Shared branded email shell for every transactional email (verification,
    password reset, and any future ones). Uses a table-based layout rather
    than flexbox/grid - email clients (particularly Outlook desktop) have
    very inconsistent CSS support, and tables are the one layout method
    that renders reliably everywhere. Colors match the site's own palette
    exactly (static/style.css --gold/--bg/--surface/--border).
    """
    body_html = "".join(f'<p style="margin:0 0 10px;font-size:14px;line-height:2;color:#a89f8c;">{line}</p>' for line in body_lines)
    return f"""
<div dir="rtl" style="background:#0f0e0c;padding:40px 16px;margin:0;font-family:Tahoma,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" align="center" style="max-width:460px;margin:0 auto;background:#1a1814;border:1px solid #2e2a22;border-radius:14px;overflow:hidden;">
    <tr>
      <td style="padding:32px 32px 4px;text-align:center;">
        <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto 22px;">
          <tr>
            <td style="width:40px;height:40px;border:1.5px solid #c9a84c;border-radius:10px;text-align:center;vertical-align:middle;color:#c9a84c;font-size:19px;font-weight:bold;font-family:Tahoma,Arial,sans-serif;">خ</td>
            <td style="padding-right:10px;color:#c9a84c;font-size:19px;font-weight:bold;font-family:Tahoma,Arial,sans-serif;">خیام</td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td style="padding:4px 36px 36px;text-align:center;">
        <h1 style="margin:0 0 16px;font-size:19px;color:#f2efe8;font-weight:700;">{heading}</h1>
        {body_html}
        <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:24px auto 0;">
          <tr>
            <td style="background:#c9a84c;border-radius:9px;">
              <a href="{button_link}" style="display:inline-block;padding:14px 40px;color:#0f0e0c;font-size:15px;font-weight:700;text-decoration:none;font-family:Tahoma,Arial,sans-serif;">{button_text}</a>
            </td>
          </tr>
        </table>
        <p style="margin:26px 0 0;font-size:12px;color:#5c574a;line-height:1.9;">
          اگر دکمه کار نکرد، این لینک را در مرورگر خود باز کنید:<br/>
          <a href="{button_link}" style="color:#8a6e2f;word-break:break-all;">{button_link}</a>
        </p>
      </td>
    </tr>
    <tr>
      <td style="padding:18px 32px;background:#141210;border-top:1px solid #2e2a22;text-align:center;">
        <p style="margin:0;font-size:12px;color:#6b6558;line-height:1.8;">{footer_text}</p>
      </td>
    </tr>
  </table>
</div>"""

def send_verification_email(user):
    if not user.email:
        return
    token = create_account_token(user.id, 'verify_email', ttl_minutes=60 * 24)
    link  = f"{SITE_URL}/verify-email?token={token}"
    html  = build_email_html(
        heading="تایید ایمیل",
        body_lines=[
            f"سلام {user.username}،",
            "برای تایید ایمیل خود روی دکمه زیر کلیک کنید.",
            "این لینک تا ۲۴ ساعت معتبر است."
        ],
        button_text="تایید ایمیل",
        button_link=link,
        footer_text="اگر این حساب را نساخته‌اید، این ایمیل را نادیده بگیرید."
    )
    send_email(user.email, "تایید ایمیل — خیام", html)

def send_password_reset_email(user):
    token = create_account_token(user.id, 'reset_password', ttl_minutes=30)
    link  = f"{SITE_URL}/reset-password?token={token}"
    html  = build_email_html(
        heading="بازیابی رمز عبور",
        body_lines=[
            f"سلام {user.username}،",
            "برای تعیین رمز عبور جدید روی دکمه زیر کلیک کنید.",
            "این لینک تا ۳۰ دقیقه معتبر است."
        ],
        button_text="تعیین رمز عبور جدید",
        button_link=link,
        footer_text="اگر این درخواست را شما نفرستاده‌اید، این ایمیل را نادیده بگیرید — رمز عبور شما تغییر نخواهد کرد."
    )
    send_email(user.email, "بازیابی رمز عبور — خیام", html)

PLAN_DISPLAY_NAMES = {
    'free': 'رایگان', 'basic': 'پایه', 'pro': 'حرفه‌ای',
    'premium': 'پریمیوم', 'tutor_pro': 'استاد حرفه‌ای',
}

def send_plan_granted_email(user, plan_type, plan_value, expires_at):
    """plan_type is 'chat' or 'tutor' - used only for the email copy."""
    if not user.email:
        return
    section_name = "چت عمومی" if plan_type == 'chat' else "بخش تدریس (استاد خیام)"
    plan_label    = PLAN_DISPLAY_NAMES.get(plan_value, plan_value)
    expiry_str    = expires_at.strftime('%Y-%m-%d') if expires_at else None
    body_lines = [
        f"سلام {user.username}،",
        f"پلان «{plan_label}» برای {section_name} روی حساب شما فعال شد.",
    ]
    if expiry_str:
        body_lines.append(f"این پلان ماهانه است و در {expiry_str} به پایان می‌رسد. برای ادامه استفاده، باید آن را دوباره تمدید کنید.")
    html = build_email_html(
        heading="پلان شما فعال شد",
        body_lines=body_lines,
        button_text="مشاهده حساب کاربری",
        button_link=f"{SITE_URL}/profile",
        footer_text="اگر سوالی دارید، با پشتیبانی خیام تماس بگیرید."
    )
    send_email(user.email, "پلان شما فعال شد — خیام", html)

def send_plan_expired_email(user, plan_type, plan_value):
    if not user.email:
        return
    section_name = "چت عمومی" if plan_type == 'chat' else "بخش تدریس (استاد خیام)"
    plan_label    = PLAN_DISPLAY_NAMES.get(plan_value, plan_value)
    html = build_email_html(
        heading="پلان شما به پایان رسید",
        body_lines=[
            f"سلام {user.username}،",
            f"پلان «{plan_label}» شما برای {section_name} به پایان رسیده و حساب شما به پلان رایگان تغییر کرد.",
            "هر وقت خواستید می‌توانید دوباره آن را فعال کنید."
        ],
        button_text="تمدید پلان",
        button_link=f"{SITE_URL}/pricing",
        footer_text="این یک اطلاع‌رسانی خودکار است."
    )
    send_email(user.email, "پلان شما به پایان رسید — خیام", html)

def grant_plan(user, plan_type, plan_value, ttl_days=30):
    """
    Sets a user's chat_plan or tutor_plan, with a monthly expiry (unless
    resetting to 'free', which has no expiry). Sends the "plan activated"
    email. This is the single place plan-granting happens, so both the
    admin panel and any future payment-webhook integration call the same
    logic instead of duplicating it.
    """
    expires_at = None if plan_value == 'free' else datetime.utcnow() + timedelta(days=ttl_days)
    if plan_type == 'chat':
        user.chat_plan = plan_value
        user.chat_plan_expires_at = expires_at
    else:
        user.tutor_plan = plan_value
        user.tutor_plan_expires_at = expires_at
    db.session.commit()

    if plan_value != 'free':
        try:
            send_plan_granted_email(user, plan_type, plan_value, expires_at)
        except Exception as e:
            print(f"send_plan_granted_email failed for user {user.id}: {e}")

def check_and_expire_plans(user):
    """
    Lazily checks whether either of a user's plans has passed its expiry
    date, downgrading it to free and emailing them if so. Called on every
    authenticated request (see before_request hook below) rather than via
    a separate cron job - the moment a user's plan has expired, the very
    next request they make (or any request while they're logged in) will
    catch and correct it, without needing a scheduler running on Render.
    """
    now = datetime.utcnow()
    changed = False

    if user.chat_plan != 'free' and user.chat_plan_expires_at and user.chat_plan_expires_at < now:
        expired_plan = user.chat_plan
        user.chat_plan = 'free'
        user.chat_plan_expires_at = None
        changed = True
        try:
            send_plan_expired_email(user, 'chat', expired_plan)
        except Exception as e:
            print(f"send_plan_expired_email (chat) failed for user {user.id}: {e}")

    if user.tutor_plan != 'free' and user.tutor_plan_expires_at and user.tutor_plan_expires_at < now:
        expired_plan = user.tutor_plan
        user.tutor_plan = 'free'
        user.tutor_plan_expires_at = None
        changed = True
        try:
            send_plan_expired_email(user, 'tutor', expired_plan)
        except Exception as e:
            print(f"send_plan_expired_email (tutor) failed for user {user.id}: {e}")

    if changed:
        db.session.commit()

PLAN_CONFIG = {
    'free': [
        {'model': 'gpt-5.4-mini',                              'tier': 1, 'limit': 5000,    'reset': 'daily'},
        {'model': 'gpt-5.4-nano',                              'tier': 2, 'limit': 10000,   'reset': 'daily'},
        {'model': 'meta-llama/llama-4-scout-17b-16e-instruct', 'tier': 3, 'limit': None,    'reset': None},
    ],
    'basic': [
        {'model': 'gpt-5.4-mini',                              'tier': 1, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',                              'tier': 2, 'limit': 200000,  'reset': 'monthly'},
        {'model': 'meta-llama/llama-4-scout-17b-16e-instruct', 'tier': 3, 'limit': None,    'reset': None},
    ],
    'pro': [
        {'model': 'gpt-5.4-mini',                              'tier': 1, 'limit': 500000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',                              'tier': 2, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'meta-llama/llama-4-scout-17b-16e-instruct', 'tier': 3, 'limit': None,    'reset': None},
    ],
    'premium': [
        {'model': 'gpt-5.4',                                   'tier': 1, 'limit': 2000000, 'reset': 'monthly'},
        {'model': 'gpt-5.4-mini',                              'tier': 2, 'limit': 500000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',                              'tier': 3, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'meta-llama/llama-4-scout-17b-16e-instruct', 'tier': 4, 'limit': None,    'reset': None},
    ],
}

PLAN_NAMES = {
    'free':    'رایگان',
    'basic':   'پایه — $10',
    'pro':     'حرفه‌ای — $20',
    'premium': 'پریمیوم — $40',
}

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ── ADMIN DECORATOR ──
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated

# ── TOKEN USAGE ──
def get_or_create_usage(user_id):
    try:
        usage = UserTokenUsage.query.filter_by(user_id=user_id).first()
        if not usage:
            usage = UserTokenUsage(
                user_id=user_id,
                tier1_tokens=0, tier1_reset=datetime.utcnow(),
                tier2_tokens=0, tier2_reset=datetime.utcnow(),
                tier3_tokens=0, tier3_reset=datetime.utcnow(),
            )
            db.session.add(usage)
            db.session.commit()
        return usage
    except Exception as e:
        print(f"get_or_create_usage error: {e}")
        db.session.rollback()
        return None

def should_reset(reset_at, period):
    if period is None or reset_at is None:
        return False
    now = datetime.utcnow()
    if period == 'daily':
        return (now - reset_at).total_seconds() >= 86400
    if period == 'monthly':
        return (now - reset_at).total_seconds() >= 2592000
    return False

def get_reset_timestamp(reset_at, period):
    if not reset_at or not period:
        return None
    if period == 'daily':
        reset_time = reset_at + timedelta(days=1)
    elif period == 'monthly':
        reset_time = reset_at + timedelta(days=30)
    else:
        return None
    return reset_time.replace(tzinfo=timezone.utc).timestamp()

def pick_model_and_update(user_id, plan, tokens_to_use):
    cascade = PLAN_CONFIG.get(plan, PLAN_CONFIG['free'])
    usage   = get_or_create_usage(user_id)

    if usage is None:
        first = cascade[0]
        return first['model'], first['tier'], None, False

    for i, tier_config in enumerate(cascade):
        model  = tier_config['model']
        limit  = tier_config['limit']
        period = tier_config['reset']
        t      = tier_config['tier']

        if limit is None:
            return model, t, None, i > 0

        tokens_used = getattr(usage, f'tier{t}_tokens', 0) or 0
        reset_at    = getattr(usage, f'tier{t}_reset', None) or datetime.utcnow()

        if should_reset(reset_at, period):
            setattr(usage, f'tier{t}_tokens', 0)
            setattr(usage, f'tier{t}_reset', datetime.utcnow())
            tokens_used = 0
            reset_at    = datetime.utcnow()

        if tokens_used < limit:
            new_count = min(tokens_used + tokens_to_use, limit)
            setattr(usage, f'tier{t}_tokens', new_count)
            usage.updated_at = datetime.utcnow()
            db.session.commit()
            reset_ts = get_reset_timestamp(reset_at, period)
            return model, t, reset_ts, i > 0

    last = cascade[-1]
    return last['model'], last['tier'], None, True

def true_up_registered_tokens(user_id, tier, estimate, actual):
    """
    Corrects a registered user's tier-N counter once we know the real token
    count from the API response, instead of leaving it at the pre-call
    estimate. Mirrors true_up_guest_tokens but for the tiered plan pools.
    """
    if user_id is None or actual is None or tier is None:
        return
    delta = actual - estimate
    if delta == 0:
        return
    try:
        usage = get_or_create_usage(user_id)
        if not usage:
            return
        col = f'tier{tier}_tokens'
        setattr(usage, col, max(0, (getattr(usage, col, 0) or 0) + delta))
        usage.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        print(f"true_up_registered_tokens error: {e}")
        db.session.rollback()

def true_up_registered_tutor_tokens(user_id, col, estimate, actual):
    if user_id is None or actual is None or col is None:
        return
    delta = actual - estimate
    if delta == 0:
        return
    try:
        usage = get_or_create_usage(user_id)
        if not usage:
            return
        tokens_col = f'{col}_tokens'
        setattr(usage, tokens_col, max(0, (getattr(usage, tokens_col, 0) or 0) + delta))
        usage.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        print(f"true_up_registered_tutor_tokens error: {e}")
        db.session.rollback()

def get_usage_summary(user_id, plan):
    cascade = PLAN_CONFIG.get(plan, PLAN_CONFIG['free'])
    usage   = get_or_create_usage(user_id)
    summary = []
    if not usage:
        return summary
    for tier in cascade:
        t      = tier['tier']
        limit  = tier['limit']
        period = tier['reset']
        if limit is None:
            continue
        tokens_used = getattr(usage, f'tier{t}_tokens', 0) or 0
        reset_at    = getattr(usage, f'tier{t}_reset', datetime.utcnow())
        if should_reset(reset_at, period):
            tokens_used = 0
        summary.append({
            'model':     tier['model'],
            'used':      tokens_used,
            'limit':     limit,
            'reset_ts':  get_reset_timestamp(reset_at, period),
            'period':    period,
            'remaining': max(0, limit - tokens_used)
        })
    return summary
# ── TUTOR TOKEN TRACKING (SEPARATE POOL) ──
TUTOR_PLAN_CONFIG = {
    'free': [
        {'model': 'gpt-5.4-mini', 'limit': 5000,    'reset': 'daily',   'col': 'tutor_tier1'},
    ],
    'basic': [
        {'model': 'gpt-5.4-mini', 'limit': 150000,  'reset': 'monthly', 'col': 'tutor_tier1'},
    ],
    'pro': [
        {'model': 'gpt-5.4-mini', 'limit': 300000,  'reset': 'monthly', 'col': 'tutor_tier1'},
    ],
    'premium': [
        {'model': 'gpt-5.4-mini', 'limit': 500000,  'reset': 'monthly', 'col': 'tutor_tier1'},
        {'model': 'gpt-5.4-nano', 'limit': 300000,  'reset': 'monthly', 'col': 'tutor_tier2'},
    ],
    'tutor_pro': [
        {'model': 'gpt-5.4',      'limit': 3000000, 'reset': 'monthly', 'col': 'tutor_tier1'},
    ],
}

# All plan values a user's `plan` column can actually hold. tutor_pro isn't
# in PLAN_CONFIG (it intentionally gets free-tier general chat - it's a
# tutor-only upgrade), so anything validating a plan value against "is this
# real" should check this set, not PLAN_CONFIG alone.
VALID_PLANS = set(PLAN_CONFIG.keys()) | set(TUTOR_PLAN_CONFIG.keys())

def pick_tutor_model(user_id, plan, tokens_to_use):
    """
    Returns (model, reset_ts, is_limited, col)
    is_limited=True means hard stop — no fallback, show upgrade message
    col is the usage column that was charged, needed later to true it up
    with the real token count once the API responds.
    """
    cascade = TUTOR_PLAN_CONFIG.get(plan, TUTOR_PLAN_CONFIG['free'])
    usage   = get_or_create_usage(user_id)
    if not usage:
        return cascade[0]['model'], None, False, None

    for tier in cascade:
        model  = tier['model']
        limit  = tier['limit']
        period = tier['reset']
        col    = tier['col']

        tokens_attr = f'{col}_tokens'
        reset_attr  = f'{col}_reset'
        tokens_used = getattr(usage, tokens_attr, 0) or 0
        reset_at    = getattr(usage, reset_attr, None) or datetime.utcnow()

        if should_reset(reset_at, period):
            setattr(usage, tokens_attr, 0)
            setattr(usage, reset_attr, datetime.utcnow())
            tokens_used = 0
            reset_at    = datetime.utcnow()

        if tokens_used < limit:
            setattr(usage, tokens_attr, min(tokens_used + tokens_to_use, limit))
            usage.updated_at = datetime.utcnow()
            db.session.commit()
            reset_ts = get_reset_timestamp(reset_at, period)
            return model, reset_ts, False, col

        # this tier is exhausted — hard stop
        reset_ts = get_reset_timestamp(reset_at, period)
        return None, reset_ts, True, None

    return None, None, True, None

def smart_tutor_chat(system_prompt, history, user_message, user_id=None, plan='free', temperature=0.7):
    """
    Returns (reply, is_limited, reset_ts)
    is_limited=True means token limit hit — frontend shows upgrade message
    (used for both the registered-plan cascade and the guest daily quota)
    """
    messages         = history + [{"role": "user", "content": user_message}]
    estimated_tokens = len(user_message) // 4 + 400

    if user_id is None:
        # guest — capped daily quota, shared with general chat, same as smart_chat
        allowed, ip_hash, date_key = reserve_guest_tokens(estimated_tokens)
        if not allowed:
            return None, True, next_utc_midnight_ts()
        try:
            reply, actual = call_openai_model(GUEST_MODEL, system_prompt, messages, temperature)
            true_up_guest_tokens(ip_hash, date_key, estimated_tokens, actual)
            return reply, False, None
        except Exception as e:
            print(f"Tutor guest error: {e}")
            return "متأسفم، مشکلی پیش آمد. لطفاً دوباره امتحان کنید.", False, None

    model, reset_ts, is_limited, col = pick_tutor_model(user_id, plan, estimated_tokens)
    print(f"TUTOR: user={user_id} plan={plan} model={model} limited={is_limited}")

    if is_limited:
        return None, True, reset_ts

    try:
        reply, actual = call_openai_model(model, system_prompt, messages, temperature)
        true_up_registered_tutor_tokens(user_id, col, estimated_tokens, actual)
        return reply, False, reset_ts
    except Exception as e:
        print(f"Tutor OpenAI error with {model}: {e}")
        return "متأسفم، مشکلی پیش آمد. لطفاً دوباره امتحان کنید.", False, reset_ts
# ── KNOWLEDGE (DATABASE BACKED) ──
def load_knowledge():
    try:
        row = SiteConfig.query.filter_by(key='knowledge').first()
        if row:
            return json.loads(row.value)
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            save_knowledge(data)
            return data
    except:
        return {"dari_dialect": [], "cultural_customs": []}

def load_examples():
    try:
        row = SiteConfig.query.filter_by(key='examples').first()
        if row:
            return json.loads(row.value)
        with open(EXAMPLES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            save_examples(data)
            return data
    except:
        return {"conversation_examples": []}

def save_knowledge(data):
    try:
        row = SiteConfig.query.filter_by(key='knowledge').first()
        if row:
            row.value      = json.dumps(data, ensure_ascii=False)
            row.updated_at = datetime.utcnow()
        else:
            row = SiteConfig(key='knowledge', value=json.dumps(data, ensure_ascii=False))
            db.session.add(row)
        db.session.commit()
    except Exception as e:
        print(f"save_knowledge error: {e}")
        db.session.rollback()

def save_examples(data):
    try:
        row = SiteConfig.query.filter_by(key='examples').first()
        if row:
            row.value      = json.dumps(data, ensure_ascii=False)
            row.updated_at = datetime.utcnow()
        else:
            row = SiteConfig(key='examples', value=json.dumps(data, ensure_ascii=False))
            db.session.add(row)
        db.session.commit()
    except Exception as e:
        print(f"save_examples error: {e}")
        db.session.rollback()

# ── MEMORY ──
def get_user_memories(user_id):
    memories = Memory.query.filter_by(
        user_id=user_id
    ).order_by(Memory.created_at.desc()).limit(20).all()
    return [m.content for m in memories]

def extract_and_save_memory(user_id, user_message):
    trigger_words = [
        'یادت باشد', 'یادت باشه', 'ذخیره کن', 'به یاد داشته باش',
        'فراموش نکن', 'همیشه بدان', 'بدان که', 'حفظ کن',
        'remember', 'save this', 'note that', 'keep in mind',
        'always know', 'dont forget', "don't forget"
    ]
    msg_lower = user_message.lower()
    if not any(word.lower() in msg_lower for word in trigger_words):
        return

    extraction_prompt = f"""کاربر این پیام را فرستاده:
"{user_message}"

اگر کاربر خواسته چیزی برای همیشه به یاد سپرده شود، آن را یک جمله کوتاه بنویس.
مثال: "کاربر اسمش احمد است" یا "کاربر پزشک است"
اگر چیزی برای ذخیره نیست فقط بنویس: NONE
فقط یک جمله یا NONE."""

    memory_text = None

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=[{"role": "user", "content": extraction_prompt}],
            max_completion_tokens=80,
            temperature=0.1
        )
        memory_text = response.choices[0].message.content.strip()
        print(f"Memory extraction (OpenAI): '{memory_text}'")
    except Exception as e:
        print(f"Memory OpenAI failed, trying Groq: {e}")
        try:
            response = groq_client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[{"role": "user", "content": extraction_prompt}],
                max_tokens=80,
                temperature=0.1
            )
            memory_text = response.choices[0].message.content.strip()
            print(f"Memory extraction (Groq): '{memory_text}'")
        except Exception as e2:
            print(f"Memory extraction failed completely: {e2}")
            return

    if memory_text and memory_text.upper() != "NONE" and len(memory_text) > 3:
        try:
            existing = Memory.query.filter_by(
                user_id=user_id, content=memory_text
            ).first()
            if not existing:
                db.session.add(Memory(user_id=user_id, content=memory_text))
                db.session.commit()
                print(f"Memory saved for user {user_id}: {memory_text}")
        except Exception as e:
            print(f"Memory DB save error: {e}")
            db.session.rollback()

# ── PROMPTS ──
def build_system_prompt(user_memories=None):
    knowledge = load_knowledge()
    examples  = load_examples()

    dialect_rules = ""
    for item in knowledge.get("dari_dialect", []):
        dialect_rules += f'- بگو "{item["correct"]}" نه "{item["wrong"]}" ({item["note"]})\n'

    cultural_knowledge = ""
    for item in knowledge.get("cultural_customs", []):
        cultural_knowledge += f'- {item["topic"]}: {item["content"]}\n'

    example_block = ""
    for ex in examples.get("conversation_examples", []):
        example_block += f'User: {ex["user"]}\nAssistant: {ex["assistant"]}\n\n'

    memory_block = ""
    if user_memories:
        memory_block = "====================\nاطلاعات ذخیره‌شده درباره این کاربر\n====================\n"
        memory_block += "این اطلاعات را همیشه در نظر بگیر و در پاسخ‌هایت استفاده کن:\n"
        for mem in user_memories:
            memory_block += f"- {mem}\n"
        memory_block += "\n"

    return f"""تو یک دستیار هوشمند به نام خیام هستی که به زبان دری افغانی صحبت می‌کنی.

{memory_block}====================
قانون زبان
====================
زبان پیش‌فرض: دری افغانی
- اگر موضوع آموزش زبان باشد، مثال‌ها را به همان زبان بنویس اما توضیحات را به دری بده
- در موضوعات علمی استفاده از نمادها مجاز است (x, H2O, km)
- اگر کاربر لینک فرستاد بگو نمی‌توانی باز کنی
- اگر کاربر تصویر یا فایل فرستاد آن را به دری توضیح بده

====================
هویت و شخصیت
====================
- نام: خیام
- لحن: گرم، مهربان و صمیمی
- از کلمات مانند: برادر، خواهر، تشکر استفاده کن
- هرگز خود را ChatGPT معرفی نکن
- اگر اطلاعاتی درباره کاربر داری از آن استفاده کن

====================
سیستم حافظه — بسیار مهم
====================
تو یک سیستم حافظه دائمی داری که اطلاعات کاربران را برای همیشه ذخیره می‌کند.

وقتی کاربر می‌گوید "یادت باشه"، "ذخیره کن"، "remember" یا مشابه:
- بگو: "بسیار خوب، این را در حافظه‌ام ذخیره کردم و در همه گفتگوهای بعدی به یاد خواهم داشت."
- هرگز نگو که نمی‌توانی چیزی را به یاد بسپاری
- هرگز نگو که حافظه‌ات فقط در این چت کار می‌کند

====================
فرمت‌بندی — بسیار مهم
====================
۱. هر پاسخ را به پاراگراف‌های کوتاه تقسیم کن — هر پاراگراف حداکثر ۳ جمله
۲. بین هر پاراگراف یک خط خالی بگذار
۳. موضوعات مختلف را با ## تیتر جدا کن
۴. برای لیست از - استفاده کن
۵. کلمات مهم را **بولد** کن
۶. جواب‌های کوتاه را بدون فرمت بنویس
۷. هرگز یک بلوک طولانی بدون تقسیم‌بندی ننویس
۸. اگر کد می‌نویسی (هر زبان برنامه‌نویسی)، همیشه آن را داخل بلوک کد با سه بک‌تیک بگذار و نام زبان را جلوی بک‌تیک اول بنویس، مثلاً ```python — کد را هرگز وسط متن معمولی و بدون بلوک ننویس، حتی یک خط کوتاه

====================
حالت ویژه: شعر
====================
- هر مصرع روی یک خط جداگانه
- بین هر دو بیت یک خط خالی
- قافیه را در تمام شعر حفظ کن
- فقط شعر بنویس — هیچ توضیح اضافه نده

====================
دانش فرهنگی
====================
{cultural_knowledge}

====================
قوانین گویش
====================
{dialect_rules}

====================
نمونه‌ها
====================
{example_block}"""

def build_tutor_prompt(subject, grade):
    return f"""تو استاد خیام هستی — یک استاد افغانی مهربان که به دری افغانی درس می‌دهی.

مضمون: {subject}
سطح: {grade}

====================
قوانین زبان
====================
فقط به دری افغانی جواب بده.
در موضوعات علمی استفاده از نمادها و فرمول‌ها مجاز است.

====================
قوانین فرمت‌بندی — بسیار مهم
====================
۱. هر موضوع جدید را در یک پاراگراف جداگانه بنویس. بین پاراگراف‌ها خط خالی بگذار.

۲. مثال را همیشه در یک پاراگراف کاملاً جدا بنویس — هرگز در وسط توضیح نگذار.
   مثال را با **مثال:** شروع کن.

۳. سوال را همیشه در آخر و در یک پاراگراف جدا بنویس.
   سوال را با **سوال:** شروع کن.

۴. برای چند نکته از لیست استفاده کن:
   - نکته اول
   - نکته دوم

نمونه فرمت صحیح:

**تعریف**

کسر عددی است از دو قسمت — صورت و مخرج. صورت نشان می‌دهد چند قسمت داریم.

**مثال:**

یک نان را به ۴ قسمت تقسیم کردیم و ۱ قسمت گرفتیم — این می‌شود ۱/۴.

**سوال:**

اگر نان را به ۸ قسمت تقسیم کنیم و ۳ قسمت بگیریم، کسر آن چیست؟

====================
روش تدریس
====================
- هر بار فقط یک مفهوم توضیح بده
- توضیح حداکثر ۳ جمله باشد
- یک مثال از زندگی روزمره افغانستان بیاور
- در آخر یک سوال کوتاه بپرس
- جواب درست: احسنت، آفرین، عالی — بعد موضوع بعدی
- جواب غلط: با مهربانی تصحیح کن و دوباره توضیح بده"""

# ── CHAT FUNCTIONS ──
def call_openai_model(model, system_prompt, messages, temperature=0.7,
                      image_b64=None, image_type=None):
    formatted = []
    for m in messages:
        if m["role"] == "user" and image_b64 and m == messages[-1]:
            formatted.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": m["content"]},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{image_type};base64,{image_b64}"
                    }}
                ]
            })
        else:
            formatted.append(m)

    is_new_model = any(x in model for x in ['gpt-5', 'o1', 'o3', 'o4'])
    token_param  = 'max_completion_tokens' if is_new_model else 'max_tokens'

    kwargs = {
        'model':    model,
        'messages': [{"role": "system", "content": system_prompt}] + formatted,
        token_param: 1000,
    }
    if not is_new_model:
        kwargs['temperature'] = temperature

    response    = openai_client.chat.completions.create(**kwargs)
    reply       = response.choices[0].message.content
    tokens_used = response.usage.total_tokens
    return reply, tokens_used

def call_groq_model(system_prompt, messages, temperature=0.7):
    msgs = [{"role": "system", "content": system_prompt}] + messages
    for model in [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "openai/gpt-oss-120b",
        "llama-3.1-8b-instant"
    ]:
        try:
            response = groq_client.chat.completions.create(
                model=model,
                messages=msgs,
                temperature=temperature,
                max_tokens=800,
                top_p=0.9
            )
            return response.choices[0].message.content, 0
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e) or "404" in str(e):
                continue
            raise e
    return "متأسفم، سرور مصروف است. لطفاً دوباره امتحان کنید.", 0

def smart_chat(system_prompt, history, user_message, user_id=None, plan='free',
               temperature=0.7, image_b64=None, image_type=None):
    messages         = history + [{"role": "user", "content": user_message}]
    estimated_tokens = len(user_message) // 4 + 400

    if user_id is None:
        allowed, ip_hash, date_key = reserve_guest_tokens(estimated_tokens)
        if not allowed:
            return None, False, next_utc_midnight_ts(), None, True
        try:
            reply, actual = call_openai_model(
                GUEST_MODEL, system_prompt, messages,
                temperature, image_b64, image_type
            )
            true_up_guest_tokens(ip_hash, date_key, estimated_tokens, actual)
            return reply, False, None, GUEST_MODEL, False
        except Exception as e:
            print(f"Guest OpenAI error: {e}")
            reply, _ = call_groq_model(system_prompt, messages, temperature)
            return reply, False, None, 'llama-4-scout', False

    model, tier, reset_ts, switched = pick_model_and_update(
        user_id, plan, estimated_tokens
    )
    print(f"DEBUG: user={user_id} plan={plan} model={model} switched={switched} reset_ts={reset_ts}")

    if 'llama' in model or 'gpt-oss' in model or 'qwen' in model:
        try:
            reply, _ = call_groq_model(system_prompt, messages, temperature)
            return reply, switched, reset_ts, model, False
        except Exception as e:
            print(f"Groq error: {e}")
            return "متأسفم، سرور مصروف است. لطفاً دوباره امتحان کنید.", switched, reset_ts, model, False

    try:
        reply, actual = call_openai_model(
            model, system_prompt, messages,
            temperature, image_b64, image_type
        )
        true_up_registered_tokens(user_id, tier, estimated_tokens, actual)
        print(f"DEBUG: OpenAI replied with {model}")
        return reply, switched, reset_ts, model, False
    except Exception as e:
        print(f"OpenAI error with {model}: {e} — falling back to Groq")
        try:
            reply, _ = call_groq_model(system_prompt, messages, temperature)
            return reply, switched, reset_ts, 'llama-4-scout', False
        except Exception as e2:
            print(f"Groq fallback failed: {e2}")
            return "متأسفم، در حال حاضر سرور مصروف است. لطفاً چند دقیقه دیگر امتحان کنید.", switched, reset_ts, 'error', False

# ── DOCUMENT EXTRACTION ──
def extract_text_from_file(file_bytes, filename):
    ext = filename.lower().split('.')[-1]
    if ext == 'txt':
        return file_bytes.decode('utf-8', errors='ignore')
    if ext == 'pdf':
        try:
            import fitz
            doc  = fitz.open(stream=file_bytes, filetype="pdf")
            text = "".join([page.get_text() for page in doc])
            return text[:8000]
        except Exception as e:
            return f"خطا در خواندن PDF: {str(e)}"
    if ext in ['doc', 'docx']:
        try:
            import docx, io
            doc  = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs])
            return text[:8000]
        except Exception as e:
            return f"خطا در خواندن Word: {str(e)}"
    return "فرمت فایل پشتیبانی نمی‌شود."

# ── AUTH ROUTES ──
@app.route("/register")
def register_page():
    if current_user.is_authenticated:
        return redirect(url_for('chat_page'))
    return render_template("register.html")

@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('chat_page'))
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route("/profile")
@login_required
def profile_page():
    return render_template("profile.html")

@app.route("/pricing")
def pricing_page():
    return render_template("pricing.html")

@app.route("/api/register", methods=["POST"])
@limiter.limit("10 per hour")
def api_register():
    data     = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    email    = (data.get("email") or "").strip() or None

    if len(username) < 3:
        return jsonify({"success": False, "error": "نام کاربری باید حداقل ۳ حرف باشد"})
    if len(password) < 6:
        return jsonify({"success": False, "error": "رمز عبور باید حداقل ۶ حرف باشد"})
    if not email:
        return jsonify({"success": False, "error": "ایمیل الزامی است"})

    user, error = register_user(username, password, email, phone=None)
    if error:
        return jsonify({"success": False, "error": error})

    try:
        send_verification_email(user)
    except Exception as e:
        print(f"verification email failed for user {user.id}: {e}")
        # registration still succeeds - they can resend from the verify-pending page

    login_user(user)
    return jsonify({"success": True})

@app.route("/api/login", methods=["POST"])
@limiter.limit("20 per hour")
def api_login():
    data       = request.get_json()
    identifier = data.get("identifier", "").strip()
    password   = data.get("password", "").strip()

    user, error = login_user_by_username(identifier, password)
    if error:
        return jsonify({"success": False, "error": error})

    login_user(user)
    return jsonify({"success": True})

# ── EMAIL VERIFICATION ──
@app.route("/verify-pending")
@login_required
def verify_pending_page():
    if current_user.is_verified:
        return redirect(url_for('chat_page'))
    return render_template("verify-pending.html", email=current_user.email)

@app.route("/verify-email")
def verify_email_page():
    token   = request.args.get("token", "")
    user_id = consume_account_token(token, "verify_email")
    if not user_id:
        return render_template("verify-email.html", success=False)
    user = User.query.get(user_id)
    if user:
        user.is_verified = True
        db.session.commit()
    return render_template("verify-email.html", success=True)

@app.route("/api/resend-verification", methods=["POST"])
@login_required
@limiter.limit("3 per hour")
def resend_verification():
    if current_user.is_verified:
        return jsonify({"success": False, "error": "ایمیل شما قبلاً تایید شده"})
    if not current_user.email:
        return jsonify({"success": False, "error": "برای این حساب ایمیلی ثبت نشده"})
    try:
        send_verification_email(current_user)
    except Exception as e:
        print(f"resend_verification error: {e}")
        return jsonify({"success": False, "error": "ارسال ایمیل ناموفق بود. دوباره امتحان کنید."})
    return jsonify({"success": True})

# ── PASSWORD RESET ──
@app.route("/forgot-password")
def forgot_password_page():
    return render_template("forgot-password.html")

@app.route("/api/forgot-password", methods=["POST"])
@limiter.limit("5 per hour")
def api_forgot_password():
    data  = request.get_json()
    email = (data.get("email") or "").strip()

    # Always return the same response whether or not the email exists -
    # confirming/denying an account's existence here is a user-enumeration
    # leak, so the UI can't tell the difference either way.
    generic_response = {"success": True, "message": "اگر این ایمیل در سیستم ثبت باشد، لینک بازیابی برایتان ارسال شد."}

    if not email:
        return jsonify(generic_response)

    user = User.query.filter_by(email=email).first()
    if user:
        try:
            send_password_reset_email(user)
        except Exception as e:
            print(f"forgot_password email error: {e}")

    return jsonify(generic_response)

@app.route("/reset-password")
def reset_password_page():
    token = request.args.get("token", "")
    return render_template("reset-password.html", token=token)

@app.route("/api/reset-password", methods=["POST"])
@limiter.limit("10 per hour")
def api_reset_password():
    data     = request.get_json()
    token    = data.get("token", "")
    password = (data.get("password") or "").strip()

    if len(password) < 6:
        return jsonify({"success": False, "error": "رمز عبور باید حداقل ۶ حرف باشد"})

    user_id = consume_account_token(token, "reset_password")
    if not user_id:
        return jsonify({"success": False, "error": "لینک نامعتبر یا منقضی شده. یک لینک جدید درخواست کنید."})

    user = User.query.get(user_id)
    if not user:
        return jsonify({"success": False, "error": "حساب کاربری یافت نشد"})

    from auth import hash_password
    user.password_hash = hash_password(password)
    user.is_verified    = True  # proving control of the inbox is as good as clicking the verify link
    db.session.commit()

    login_user(user)
    return jsonify({"success": True})

@app.route("/api/me")
def api_me():
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True,
            "username":  current_user.username,
            "email":     current_user.email,
            "phone":     current_user.phone,
            "is_admin":  current_user.is_admin,
            "is_verified": bool(current_user.is_verified),
            "chat_plan":  current_user.chat_plan or 'free',
            "chat_plan_expires_at": current_user.chat_plan_expires_at.isoformat() if current_user.chat_plan_expires_at else None,
            "tutor_plan": current_user.tutor_plan or 'free',
            "tutor_plan_expires_at": current_user.tutor_plan_expires_at.isoformat() if current_user.tutor_plan_expires_at else None,
        })
    return jsonify({"logged_in": False})

@app.route("/api/usage")
@login_required
def api_usage():
    summary = get_usage_summary(current_user.id, current_user.chat_plan)
    return jsonify({
        "plan":    current_user.chat_plan,
        "summary": summary
    })

# ── MAIN ROUTES ──
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/chat")
def chat_page():
    return render_template("chat.html")

@app.route("/figures")
def figures_page():
    return render_template("figures.html")

@app.route("/tutor")
def tutor_page():
    return render_template("tutor.html")

@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    return render_template("admin_panel.html")

# ── CHAT API ──
@app.route("/api/chat", methods=["POST"])
@limiter.limit("15 per minute")
def chat():
    if request.content_type and 'multipart/form-data' in request.content_type:
        user_message = request.form.get("message", "")
        history      = json.loads(request.form.get("history", "[]"))
        conv_id      = request.form.get("conversation_id")
        conv_id      = int(conv_id) if conv_id else None
        image_b64    = None
        image_type   = None

        if 'image' in request.files:
            img          = request.files['image']
            img_bytes    = img.read()
            image_b64    = base64.b64encode(img_bytes).decode('utf-8')
            image_type   = img.content_type or 'image/jpeg'
            user_message = user_message or "این تصویر را به دری توضیح بده"

        if 'document' in request.files:
            doc          = request.files['document']
            doc_bytes    = doc.read()
            doc_text     = extract_text_from_file(doc_bytes, doc.filename)
            user_message = (user_message or "این سند را خلاصه کن") + \
                           f"\n\n[محتوای فایل]:\n{doc_text}"
    else:
        data         = request.get_json()
        user_message = data.get("message", "")
        history      = data.get("history", [])
        conv_id      = data.get("conversation_id")
        image_b64    = None
        image_type   = None

    user_id = current_user.id if current_user.is_authenticated else None
    plan    = getattr(current_user, 'chat_plan', 'free') or 'free' \
              if current_user.is_authenticated else 'free'

    user_memories = get_user_memories(user_id) if user_id else None

    try:
        reply, switched, reset_ts, model_used, is_limited = smart_chat(
            system_prompt=build_system_prompt(user_memories=user_memories),
            history=history[-10:],
            user_message=user_message,
            user_id=user_id,
            plan=plan,
            temperature=0.7,
            image_b64=image_b64,
            image_type=image_type
        )
    except Exception as e:
        print(f"smart_chat crashed: {e}")
        return jsonify({"reply": "متأسفم، خطایی رخ داد. لطفاً دوباره امتحان کنید."})

    # guest daily quota exhausted — no reply, frontend shows signup/limit notice
    if is_limited:
        return jsonify({"limited": True, "reset_ts": reset_ts, "guest": True})

    if not reply:
        reply = "متأسفم، پاسخی دریافت نشد. لطفاً دوباره امتحان کنید."

    if user_id:
        extract_and_save_memory(user_id, user_message)

    response_data = {"reply": reply}

    if switched:
        response_data["switch_notice"] = True
        if reset_ts:
            response_data["reset_ts"] = reset_ts

    if current_user.is_authenticated:
        conv = None
        if conv_id:
            conv = Conversation.query.filter_by(
                id=conv_id, user_id=current_user.id
            ).first()

        if not conv:
            title = user_message[:60] if user_message else "گفتگوی جدید"
            conv  = Conversation(user_id=current_user.id, title=title)
            db.session.add(conv)
            db.session.flush()

        db.session.add(Message(
            conversation_id=conv.id, role='user', content=user_message
        ))
        db.session.add(Message(
            conversation_id=conv.id, role='assistant', content=reply
        ))
        conv.updated_at = datetime.utcnow()
        db.session.commit()
        response_data["conversation_id"] = conv.id

    return jsonify(response_data)



# ── PERSONA API ──
@app.route("/api/persona-chat", methods=["POST"])
@limiter.limit("15 per minute")
def persona_chat():
    data           = request.get_json()
    user_message   = data.get("message", "")
    history        = data.get("history", [])
    persona_prompt = data.get("persona_prompt", "")

    user_id = current_user.id if current_user.is_authenticated else None
    plan    = getattr(current_user, 'chat_plan', 'free') or 'free' \
              if current_user.is_authenticated else 'free'

    try:
        reply, _, reset_ts, _, is_limited = smart_chat(
            system_prompt=persona_prompt,
            history=history[-10:],
            user_message=user_message,
            user_id=user_id,
            plan=plan,
            temperature=0.9
        )
    except Exception as e:
        print(f"Persona chat error: {e}")
        return jsonify({"reply": "متأسفم، مشکلی پیش آمد."})

    if is_limited:
        return jsonify({"limited": True, "reset_ts": reset_ts, "guest": True})

    return jsonify({"reply": reply})

# ── CONVERSATION APIs ──
@app.route("/api/conversations", methods=["GET"])
@login_required
def get_conversations():
    convs = Conversation.query.filter_by(
        user_id=current_user.id
    ).order_by(Conversation.updated_at.desc()).all()
    return jsonify([{
        "id":         c.id,
        "title":      c.title,
        "updated_at": c.updated_at.isoformat()
    } for c in convs])

@app.route("/api/conversations/<int:conv_id>", methods=["GET"])
@login_required
def get_conversation(conv_id):
    conv = Conversation.query.filter_by(
        id=conv_id, user_id=current_user.id
    ).first_or_404()
    return jsonify(conv.to_dict())

@app.route("/api/conversations/<int:conv_id>", methods=["DELETE"])
@login_required
def delete_conversation(conv_id):
    conv = Conversation.query.filter_by(
        id=conv_id, user_id=current_user.id
    ).first_or_404()
    db.session.delete(conv)
    db.session.commit()
    return jsonify({"success": True})

# ── MEMORY APIs ──
@app.route("/api/memories", methods=["GET"])
@login_required
def get_memories():
    memories = Memory.query.filter_by(
        user_id=current_user.id
    ).order_by(Memory.created_at.desc()).all()
    return jsonify([m.to_dict() for m in memories])

@app.route("/api/memories", methods=["POST"])
@login_required
def add_memory():
    data    = request.get_json()
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"success": False, "error": "محتوا خالی است"})
    memory = Memory(user_id=current_user.id, content=content)
    db.session.add(memory)
    db.session.commit()
    return jsonify({"success": True, "memory": memory.to_dict()})

@app.route("/api/memories/<int:memory_id>", methods=["DELETE"])
@login_required
def delete_memory(memory_id):
    memory = Memory.query.filter_by(
        id=memory_id, user_id=current_user.id
    ).first_or_404()
    db.session.delete(memory)
    db.session.commit()
    return jsonify({"success": True})

# ── ADMIN APIs ──
SUPPORT_PHONE = "+93782408793"

# ── VOICE TEST (temporary - admin only, for evaluating gpt-realtime's Dari
# output before deciding whether to build the real voice feature on it) ──
REALTIME_VOICE_MODEL = "gpt-realtime-2025-08-28"

@app.route("/voice-test")
@login_required
@admin_required
def voice_test_page():
    return render_template("voice-test.html")

@app.route("/api/voice-test/token", methods=["POST"])
@login_required
@admin_required
@limiter.limit("20 per hour")
def voice_test_token():
    """
    Mints a short-lived ephemeral token so the browser can connect directly
    to OpenAI's Realtime API without ever seeing the real API key. This is
    the newer (post-GA) token endpoint - if OpenAI has since renamed it
    again, the error text returned here will say so directly rather than
    failing silently.
    """
    body = json.dumps({
        "session": {
            "type": "realtime",
            "model": REALTIME_VOICE_MODEL,
            "instructions": (
                "شما باید همیشه و فقط به زبان دری (فارسی افغانستان) صحبت کنید. "
                "هر متنی که کاربر می‌فرستد را با صدای طبیعی و روان به دری بخوانید، "
                "و اگر خواسته شد یک پاسخ کوتاه و طبیعی هم بدهید."
            ),
            "audio": {"output": {"voice": "cedar"}}
        }
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=body,
        headers={
            "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return jsonify({"success": True, "client_secret": data.get("value")})
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        print(f"voice_test_token HTTP {e.code}: {error_body}")
        return jsonify({"success": False, "error": f"HTTP {e.code}: {error_body}"})
    except Exception as e:
        print(f"voice_test_token error: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/call-request", methods=["POST"])
@limiter.limit("5 per hour")
def submit_call_request():
    data           = request.get_json()
    name           = (data.get("name") or "").strip()
    phone          = (data.get("phone") or "").strip()
    requested_plan = (data.get("plan") or "").strip()

    if not name:
        return jsonify({"success": False, "error": "نام الزامی است"})
    if not phone:
        return jsonify({"success": False, "error": "شماره تماس الزامی است"})
    if requested_plan not in VALID_PLANS or requested_plan == 'free':
        return jsonify({"success": False, "error": "پلان نامعتبر"})

    req = CallRequest(
        user_id=current_user.id if current_user.is_authenticated else None,
        name=name, phone=phone, requested_plan=requested_plan
    )
    db.session.add(req)
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/admin/call-requests", methods=["GET"])
@login_required
@admin_required
def get_call_requests():
    reqs = CallRequest.query.order_by(CallRequest.created_at.desc()).all()
    return jsonify([{
        "id":             r.id,
        "name":           r.name,
        "phone":          r.phone,
        "requested_plan": r.requested_plan,
        "status":         r.status,
        "created_at":     r.created_at.isoformat()
    } for r in reqs])

@app.route("/api/admin/call-requests/<int:request_id>/status", methods=["POST"])
@login_required
@admin_required
def update_call_request_status(request_id):
    data   = request.get_json()
    status = data.get("status", "pending")
    if status not in ('pending', 'contacted', 'done'):
        return jsonify({"success": False, "error": "وضعیت نامعتبر"})
    req = CallRequest.query.get_or_404(request_id)
    req.status = status
    db.session.commit()
    return jsonify({"success": True})

@app.route("/api/admin/users", methods=["GET"])
@login_required
@admin_required
def get_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return jsonify([{
        "id":         u.id,
        "username":   u.username,
        "email":      u.email,
        "phone":      u.phone,
        "is_admin":   u.is_admin,
        "chat_plan":              u.chat_plan,
        "chat_plan_expires_at":   u.chat_plan_expires_at.isoformat() if u.chat_plan_expires_at else None,
        "tutor_plan":             u.tutor_plan,
        "tutor_plan_expires_at":  u.tutor_plan_expires_at.isoformat() if u.tutor_plan_expires_at else None,
        "created_at": u.created_at.isoformat()
    } for u in users])

CHAT_PLAN_VALUES  = {'free', 'basic', 'pro', 'premium'}
TUTOR_PLAN_VALUES = {'free', 'tutor_pro'}

@app.route("/api/admin/users/<int:user_id>/chat-plan", methods=["POST"])
@login_required
@admin_required
def update_user_chat_plan(user_id):
    data = request.get_json()
    plan = data.get("plan", "free")
    if plan not in CHAT_PLAN_VALUES:
        return jsonify({"success": False, "error": "پلان نامعتبر"})
    user = User.query.get_or_404(user_id)
    grant_plan(user, 'chat', plan)
    return jsonify({"success": True})

@app.route("/api/admin/users/<int:user_id>/tutor-plan", methods=["POST"])
@login_required
@admin_required
def update_user_tutor_plan(user_id):
    data = request.get_json()
    plan = data.get("plan", "free")
    if plan not in TUTOR_PLAN_VALUES:
        return jsonify({"success": False, "error": "پلان نامعتبر"})
    user = User.query.get_or_404(user_id)
    grant_plan(user, 'tutor', plan)
    return jsonify({"success": True})

@app.route("/api/admin/knowledge", methods=["GET"])
@login_required
@admin_required
def get_knowledge():
    return jsonify(load_knowledge())

@app.route("/api/admin/examples", methods=["GET"])
@login_required
@admin_required
def get_examples():
    return jsonify(load_examples())

@app.route("/api/admin/dialect", methods=["POST"])
@login_required
@admin_required
def add_dialect():
    data      = request.get_json()
    knowledge = load_knowledge()
    knowledge["dari_dialect"].append({
        "correct": data["correct"],
        "wrong":   data["wrong"],
        "note":    data["note"]
    })
    save_knowledge(knowledge)
    return jsonify({"success": True})

@app.route("/api/admin/dialect/<int:index>", methods=["DELETE"])
@login_required
@admin_required
def delete_dialect(index):
    knowledge = load_knowledge()
    if 0 <= index < len(knowledge["dari_dialect"]):
        knowledge["dari_dialect"].pop(index)
        save_knowledge(knowledge)
    return jsonify({"success": True})

@app.route("/api/admin/culture", methods=["POST"])
@login_required
@admin_required
def add_culture():
    data      = request.get_json()
    knowledge = load_knowledge()
    knowledge["cultural_customs"].append({
        "topic":   data["topic"],
        "content": data["content"]
    })
    save_knowledge(knowledge)
    return jsonify({"success": True})

@app.route("/api/admin/culture/<int:index>", methods=["DELETE"])
@login_required
@admin_required
def delete_culture(index):
    knowledge = load_knowledge()
    if 0 <= index < len(knowledge["cultural_customs"]):
        knowledge["cultural_customs"].pop(index)
        save_knowledge(knowledge)
    return jsonify({"success": True})

@app.route("/api/admin/example", methods=["POST"])
@login_required
@admin_required
def add_example():
    data     = request.get_json()
    examples = load_examples()
    examples["conversation_examples"].append({
        "user":      data["user"],
        "assistant": data["assistant"]
    })
    save_examples(examples)
    return jsonify({"success": True})

@app.route("/api/admin/example/<int:index>", methods=["DELETE"])
@login_required
@admin_required
def delete_example(index):
    examples = load_examples()
    if 0 <= index < len(examples["conversation_examples"]):
        examples["conversation_examples"].pop(index)
        save_examples(examples)
    return jsonify({"success": True})

# ══════════════════════════════════════════
# ── TUTOR CURRICULUM ──
# ══════════════════════════════════════════

CURRICULUM = {
    'math': {
        'name': 'ریاضی',
        'emoji': '🔢',
        'levels': {
            1: {
                'title': 'مبتدی',
                'topics': [
                    {'key': 'math_1_1', 'title': 'اعداد و شمارش', 'desc': 'آشنایی با اعداد ۱ تا ۱۰۰۰'},
                    {'key': 'math_1_2', 'title': 'جمع پایه', 'desc': 'جمع اعداد یک و دو رقمی'},
                    {'key': 'math_1_3', 'title': 'تفریق پایه', 'desc': 'تفریق اعداد یک و دو رقمی'},
                    {'key': 'math_1_4', 'title': 'اشکال هندسی', 'desc': 'مثلث، مربع، دایره و مستطیل'},
                    {'key': 'math_1_5', 'title': 'اندازه‌گیری پایه', 'desc': 'طول، وزن و زمان'},
                ]
            },
            2: {
                'title': 'ابتدایی',
                'topics': [
                    {'key': 'math_2_1', 'title': 'ضرب', 'desc': 'جدول ضرب و ضرب اعداد'},
                    {'key': 'math_2_2', 'title': 'تقسیم', 'desc': 'تقسیم اعداد و باقیمانده'},
                    {'key': 'math_2_3', 'title': 'کسرها', 'desc': 'کسر معمولی و مقایسه کسرها'},
                    {'key': 'math_2_4', 'title': 'اعشار', 'desc': 'اعداد اعشاری و عملیات با آن'},
                    {'key': 'math_2_5', 'title': 'هندسه ابتدایی', 'desc': 'محیط و مساحت اشکال'},
                    {'key': 'math_2_6', 'title': 'نمودارها', 'desc': 'خواندن و رسم نمودار'},
                ]
            },
            3: {
                'title': 'متوسط',
                'topics': [
                    {'key': 'math_3_1', 'title': 'معادلات خطی', 'desc': 'حل معادلات با یک مجهول'},
                    {'key': 'math_3_2', 'title': 'نسبت و تناسب', 'desc': 'نسبت، تناسب و کاربرد'},
                    {'key': 'math_3_3', 'title': 'درصد', 'desc': 'محاسبه درصد و کاربرد'},
                    {'key': 'math_3_4', 'title': 'آمار پایه', 'desc': 'میانگین، میانه و نما'},
                    {'key': 'math_3_5', 'title': 'احتمال پایه', 'desc': 'مفهوم احتمال و محاسبه'},
                    {'key': 'math_3_6', 'title': 'هندسه متوسط', 'desc': 'قضیه فیثاغورس و زوایا'},
                ]
            },
            4: {
                'title': 'پیشرفته',
                'topics': [
                    {'key': 'math_4_1', 'title': 'جبر', 'desc': 'معادلات، نامعادلات و دستگاه'},
                    {'key': 'math_4_2', 'title': 'توابع', 'desc': 'تابع خطی، درجه دوم و نمودار'},
                    {'key': 'math_4_3', 'title': 'مثلثات', 'desc': 'sin, cos, tan و کاربرد'},
                    {'key': 'math_4_4', 'title': 'حساب دیفرانسیل مقدماتی', 'desc': 'مشتق و انتگرال پایه'},
                    {'key': 'math_4_5', 'title': 'آمار پیشرفته', 'desc': 'انحراف معیار، توزیع نرمال'},
                ]
            }
        }
    },
    'science': {
        'name': 'علوم',
        'emoji': '🔬',
        'levels': {
            1: {
                'title': 'مبتدی',
                'topics': [
                    {'key': 'sci_1_1', 'title': 'موجودات زنده', 'desc': 'حیوانات، گیاهان و تفاوت آن‌ها'},
                    {'key': 'sci_1_2', 'title': 'بدن انسان پایه', 'desc': 'اعضای اصلی بدن'},
                    {'key': 'sci_1_3', 'title': 'گیاهان', 'desc': 'رشد گیاه، فتوسنتز ساده'},
                    {'key': 'sci_1_4', 'title': 'زمین و آسمان', 'desc': 'روز و شب، فصول، ستارگان'},
                    {'key': 'sci_1_5', 'title': 'مواد', 'desc': 'جامد، مایع و گاز'},
                ]
            },
            2: {
                'title': 'ابتدایی',
                'topics': [
                    {'key': 'sci_2_1', 'title': 'سلول', 'desc': 'واحد اساسی حیات'},
                    {'key': 'sci_2_2', 'title': 'نیرو و حرکت', 'desc': 'نیرو، سرعت و جاذبه'},
                    {'key': 'sci_2_3', 'title': 'انرژی', 'desc': 'انواع انرژی و تبدیل'},
                    {'key': 'sci_2_4', 'title': 'آب و هوا', 'desc': 'چرخه آب، آب و هوا'},
                    {'key': 'sci_2_5', 'title': 'اکوسیستم', 'desc': 'زنجیره غذایی و محیط زیست'},
                ]
            },
            3: {
                'title': 'متوسط',
                'topics': [
                    {'key': 'sci_3_1', 'title': 'شیمی پایه', 'desc': 'اتم، مولکول، مواد و تغییرات'},
                    {'key': 'sci_3_2', 'title': 'برق پایه', 'desc': 'مدار، ولت، آمپر'},
                    {'key': 'sci_3_3', 'title': 'موج و صدا', 'desc': 'موج، فرکانس، صدا و نور'},
                    {'key': 'sci_3_4', 'title': 'وراثت', 'desc': 'ژن، DNA و وراثت'},
                    {'key': 'sci_3_5', 'title': 'منظومه شمسی', 'desc': 'سیارات، ماه و ستارگان'},
                ]
            },
            4: {
                'title': 'پیشرفته',
                'topics': [
                    {'key': 'sci_4_1', 'title': 'شیمی پیشرفته', 'desc': 'جدول تناوبی، پیوند کیمیاوی'},
                    {'key': 'sci_4_2', 'title': 'فزیک پیشرفته', 'desc': 'قوانین نیوتن، ترمودینامیک'},
                    {'key': 'sci_4_3', 'title': 'بیولوژی پیشرفته', 'desc': 'تکامل، سیستم‌های بدن'},
                    {'key': 'sci_4_4', 'title': 'تکامل', 'desc': 'نظریه داروین و شواهد'},
                    {'key': 'sci_4_5', 'title': 'واکنش‌های کیمیاوی', 'desc': 'معادلات و موازنه'},
                ]
            }
        }
    },
    'dari': {
        'name': 'زبان دری',
        'emoji': '📖',
        'levels': {
            1: {
                'title': 'مبتدی',
                'topics': [
                    {'key': 'dari_1_1', 'title': 'الفبا و حروف', 'desc': 'حروف دری و تلفظ'},
                    {'key': 'dari_1_2', 'title': 'کلمات پایه', 'desc': 'واژگان روزمره'},
                    {'key': 'dari_1_3', 'title': 'جملات ساده', 'desc': 'ساختار جمله پایه'},
                    {'key': 'dari_1_4', 'title': 'خواندن متون ساده', 'desc': 'متون کوتاه و درک'},
                ]
            },
            2: {
                'title': 'ابتدایی',
                'topics': [
                    {'key': 'dari_2_1', 'title': 'دستور زبان پایه', 'desc': 'اسم، فعل، صفت'},
                    {'key': 'dari_2_2', 'title': 'فعل‌ها', 'desc': 'فعل حال، گذشته، آینده'},
                    {'key': 'dari_2_3', 'title': 'صفت‌ها و قیدها', 'desc': 'توصیف و تعریف'},
                    {'key': 'dari_2_4', 'title': 'نوشتن پایه', 'desc': 'انشای ساده'},
                    {'key': 'dari_2_5', 'title': 'درک مطلب', 'desc': 'خواندن و پاسخ سوال'},
                ]
            },
            3: {
                'title': 'متوسط',
                'topics': [
                    {'key': 'dari_3_1', 'title': 'دستور زبان پیشرفته', 'desc': 'جمله مرکب، وابسته'},
                    {'key': 'dari_3_2', 'title': 'شعر دری مقدماتی', 'desc': 'رباعی، دوبیتی و مثنوی'},
                    {'key': 'dari_3_3', 'title': 'نوشتن انشا', 'desc': 'انشای توصیفی و روایی'},
                    {'key': 'dari_3_4', 'title': 'ادبیات کلاسیک', 'desc': 'رودکی، فردوسی، خیام'},
                    {'key': 'dari_3_5', 'title': 'مکالمه پیشرفته', 'desc': 'مکالمه رسمی و غیررسمی'},
                ]
            },
            4: {
                'title': 'پیشرفته',
                'topics': [
                    {'key': 'dari_4_1', 'title': 'تحلیل ادبی', 'desc': 'تحلیل شعر و نثر'},
                    {'key': 'dari_4_2', 'title': 'شعر کلاسیک', 'desc': 'خیام، حافظ، مولانا'},
                    {'key': 'dari_4_3', 'title': 'نوشتن رسمی', 'desc': 'نامه، گزارش، مقاله'},
                    {'key': 'dari_4_4', 'title': 'ادبیات معاصر', 'desc': 'نویسندگان معاصر افغان'},
                ]
            }
        }
    },
    'english': {
        'name': 'انگلیسی',
        'emoji': '🇬🇧',
        'levels': {
            1: {
                'title': 'مبتدی',
                'topics': [
                    {'key': 'eng_1_1', 'title': 'الفبای انگلیسی', 'desc': 'حروف A-Z و تلفظ'},
                    {'key': 'eng_1_2', 'title': 'کلمات روزمره', 'desc': 'واژگان پایه انگلیسی'},
                    {'key': 'eng_1_3', 'title': 'سلام و احوال‌پرسی', 'desc': 'Hello, How are you'},
                    {'key': 'eng_1_4', 'title': 'اعداد و رنگ‌ها', 'desc': 'Numbers 1-100, Colors'},
                    {'key': 'eng_1_5', 'title': 'جملات ساده', 'desc': 'I am, You are, This is'},
                ]
            },
            2: {
                'title': 'ابتدایی',
                'topics': [
                    {'key': 'eng_2_1', 'title': 'گرامر پایه', 'desc': 'Nouns, Verbs, Adjectives'},
                    {'key': 'eng_2_2', 'title': 'فعل‌های اساسی', 'desc': 'To be, To have, To do'},
                    {'key': 'eng_2_3', 'title': 'زمان حال', 'desc': 'Present Simple and Continuous'},
                    {'key': 'eng_2_4', 'title': 'زمان گذشته', 'desc': 'Past Simple and Regular verbs'},
                    {'key': 'eng_2_5', 'title': 'مکالمه روزمره', 'desc': 'Shopping, Directions, Food'},
                ]
            },
            3: {
                'title': 'متوسط',
                'topics': [
                    {'key': 'eng_3_1', 'title': 'زمان آینده', 'desc': 'Will, Going to, Future plans'},
                    {'key': 'eng_3_2', 'title': 'فعل‌های کمکی', 'desc': 'Can, Could, Should, Must'},
                    {'key': 'eng_3_3', 'title': 'پرسش و جواب', 'desc': 'WH questions and answers'},
                    {'key': 'eng_3_4', 'title': 'خواندن متون', 'desc': 'Reading comprehension'},
                    {'key': 'eng_3_5', 'title': 'نوشتن پایه', 'desc': 'Paragraphs and short essays'},
                ]
            },
            4: {
                'title': 'پیشرفته',
                'topics': [
                    {'key': 'eng_4_1', 'title': 'گرامر پیشرفته', 'desc': 'Conditionals, Passive voice'},
                    {'key': 'eng_4_2', 'title': 'نوشتن رسمی', 'desc': 'Formal emails and essays'},
                    {'key': 'eng_4_3', 'title': 'درک مطلب پیشرفته', 'desc': 'Advanced reading skills'},
                    {'key': 'eng_4_4', 'title': 'مکالمه پیشرفته', 'desc': 'Fluent conversation practice'},
                    {'key': 'eng_4_5', 'title': 'آمادگی آیلتس', 'desc': 'IELTS basics and tips'},
                ]
            }
        }
    },
    'computer': {
        'name': 'کمپیوتر',
        'emoji': '💻',
        'levels': {
            1: {
                'title': 'مبتدی',
                'topics': [
                    {'key': 'comp_1_1', 'title': 'آشنایی با کمپیوتر', 'desc': 'CPU، RAM، حافظه'},
                    {'key': 'comp_1_2', 'title': 'اینترنت پایه', 'desc': 'مرورگر، جستجو، ایمیل'},
                    {'key': 'comp_1_3', 'title': 'تایپ و کیبورد', 'desc': 'تایپ سریع و میانبرها'},
                    {'key': 'comp_1_4', 'title': 'مایکروسافت ورد', 'desc': 'ایجاد و فرمت‌بندی سند'},
                    {'key': 'comp_1_5', 'title': 'مایکروسافت اکسل', 'desc': 'جداول و فرمول‌های ساده'},
                ]
            },
            2: {
                'title': 'ابتدایی',
                'topics': [
                    {'key': 'comp_2_1', 'title': 'آفیس پیشرفته', 'desc': 'PowerPoint و مهارت‌های آفیس'},
                    {'key': 'comp_2_2', 'title': 'امنیت آنلاین', 'desc': 'رمز عبور، فیشینگ، امنیت'},
                    {'key': 'comp_2_3', 'title': 'مقدمه برنامه‌نویسی', 'desc': 'منطق برنامه‌نویسی با Scratch'},
                    {'key': 'comp_2_4', 'title': 'مقدمه HTML', 'desc': 'تگ‌های اساسی HTML'},
                    {'key': 'comp_2_5', 'title': 'مقدمه CSS', 'desc': 'استایل و رنگ‌بندی وب'},
                ]
            },
            3: {
                'title': 'متوسط',
                'topics': [
                    {'key': 'comp_3_1', 'title': 'HTML و CSS کامل', 'desc': 'صفحه وب کامل'},
                    {'key': 'comp_3_2', 'title': 'Python مقدماتی', 'desc': 'متغیر، حلقه، شرط'},
                    {'key': 'comp_3_3', 'title': 'منطق برنامه‌نویسی', 'desc': 'الگوریتم و pseudocode'},
                    {'key': 'comp_3_4', 'title': 'پایگاه داده پایه', 'desc': 'SQL و جداول داده'},
                    {'key': 'comp_3_5', 'title': 'طراحی وب', 'desc': 'responsive design'},
                ]
            },
            4: {
                'title': 'پیشرفته',
                'topics': [
                    {'key': 'comp_4_1', 'title': 'Python پیشرفته', 'desc': 'تابع، کلاس، کتابخانه'},
                    {'key': 'comp_4_2', 'title': 'JavaScript پایه', 'desc': 'DOM، رویداد، AJAX'},
                    {'key': 'comp_4_3', 'title': 'پروژه‌های وب', 'desc': 'ساخت وب‌سایت کامل'},
                    {'key': 'comp_4_4', 'title': 'هوش مصنوعی مقدماتی', 'desc': 'ML، chatbot، کاربرد AI'},
                    {'key': 'comp_4_5', 'title': 'امنیت سایبری', 'desc': 'هک اخلاقی، آسیب‌پذیری'},
                ]
            }
        }
    }
}

XP_REWARDS = {
    'topic_complete': 15,
    'quiz_pass':      50,
    'quiz_perfect':   100,
    'level_complete': 200,
    'subject_complete': 500,
    'daily_login': 10,
}

BADGES = {
    'first_lesson':  {'title': 'اولین درس', 'emoji': '🎯'},
    'level_complete': {'title': 'یک سطح کامل', 'emoji': '📚'},
    'quiz_perfect':  {'title': 'نمره کامل', 'emoji': '⭐'},
    'subject_done':  {'title': 'مضمون کامل', 'emoji': '🏆'},
    'streak_3':      {'title': 'سه روز متوالی', 'emoji': '🔥'},
}

def get_or_create_progress(user_id, subject):
    progress = TutorProgress.query.filter_by(
        user_id=user_id, subject=subject
    ).first()
    if not progress:
        progress = TutorProgress(user_id=user_id, subject=subject)
        db.session.add(progress)
        db.session.commit()
    return progress

def get_or_create_topic_chat(user_id, subject, topic_key):
    """Chat history scoped to one specific topic - see TutorTopicChat."""
    chat = TutorTopicChat.query.filter_by(
        user_id=user_id, subject=subject, topic_key=topic_key
    ).first()
    if not chat:
        chat = TutorTopicChat(user_id=user_id, subject=subject, topic_key=topic_key)
        db.session.add(chat)
        db.session.commit()
    return chat

def award_xp(user_id, amount, subject=None):
    user = User.query.get(user_id)
    if user:
        user.total_xp = (user.total_xp or 0) + amount
        db.session.commit()
    if subject:
        progress = get_or_create_progress(user_id, subject)
        progress.subject_xp = (progress.subject_xp or 0) + amount
        db.session.commit()

def award_badge(user_id, badge_key, subject=None):
    existing = StudentBadge.query.filter_by(
        user_id=user_id, badge_key=badge_key, subject=subject
    ).first()
    if not existing and badge_key in BADGES:
        badge_info = BADGES[badge_key]
        badge = StudentBadge(
            user_id=user_id,
            badge_key=badge_key,
            badge_title=badge_info['title'],
            badge_emoji=badge_info['emoji'],
            subject=subject
        )
        db.session.add(badge)
        db.session.commit()
        return badge_info
    return None

def build_tutor_system_prompt(subject, topic_title, topic_desc, chat_history_len):
    subject_data = CURRICULUM.get(subject, {})
    subject_name = subject_data.get('name', subject)

    return f"""تو استاد خیام هستی — یک استاد مهربان، باهوش و خلاق که به زبان دری افغانی درس می‌دهی.

مضمون فعلی: {subject_name}
موضوع فعلی: {topic_title}
توضیح موضوع: {topic_desc}

====================
قوانین تدریس
====================
تو مثل یک استاد واقعی تدریس می‌کنی — نه یک ربات با فرمت ثابت.

- هر بار که کاربر چیزی می‌پرسد یا جواب می‌دهد، پاسخت را با درک عمیق از سطح او بده
- گاهی با یک سوال شروع کن تا بفهمی چقدر می‌داند
- گاهی با یک داستان یا مثال جالب شروع کن
- گاهی مستقیم توضیح بده
- همیشه انعطاف داشته باش — اگر کاربر گیج شد، از زاویه دیگری توضیح بده
- اگر کاربر اشتباه کرد، با مهربانی و بدون قضاوت تصحیح کن
- وقتی کاربر چیزی را درست فهمید، صادقانه تشویقش کن
- از مثال‌های زندگی روزمره افغانستان استفاده کن

====================
قوانین فرمت
====================
- پاسخ‌های کوتاه و واضح بنویس
- بین بخش‌های مختلف خط خالی بگذار
- اگر مثال داری آن را در یک خط جدا بنویس
- اگر سوال داری آن را در آخر و جدا بنویس
- از Markdown برای بولد و لیست استفاده کن
- در موضوعات علمی و کمپیوتر از نمادها و کد استفاده کن
- اگر کد برنامه‌نویسی می‌نویسی، همیشه آن را داخل بلوک کد با سه بک‌تیک و نام زبان بگذار (مثلاً ```python) — کد را هرگز وسط متن معمولی ننویس

====================
وضعیت درس
====================
{"این شروع درس است — با یک معرفی جذاب شروع کن" if chat_history_len == 0 else "درس در حال جریان است — ادامه بده"}

فقط درس بده. اگر کاربر از موضوع خارج شد، آرام او را به موضوع برگردان."""

# ══════════════════════════════════════════
# ── TUTOR API ROUTES ──
# ══════════════════════════════════════════

@app.route("/api/tutor/curriculum")
def get_curriculum():
    """Returns full curriculum structure for the frontend."""
    result = {}
    for subj_key, subj_data in CURRICULUM.items():
        result[subj_key] = {
            'name':   subj_data['name'],
            'emoji':  subj_data['emoji'],
            'levels': {}
        }
        for level_num, level_data in subj_data['levels'].items():
            result[subj_key]['levels'][str(level_num)] = {
                'title':  level_data['title'],
                'topics': level_data['topics']
            }
    return jsonify(result)

@app.route("/api/tutor/progress/<subject>")
@login_required
def get_tutor_progress(subject):
    """Returns student's progress for a specific subject (level, XP, completed topics - NOT chat history, which is per-topic; see /api/tutor/topic-chat)."""
    progress = get_or_create_progress(current_user.id, subject)
    user = User.query.get(current_user.id)
    badges = StudentBadge.query.filter_by(user_id=current_user.id).all()

    return jsonify({
        'progress':   progress.to_dict(),
        'total_xp':   user.total_xp or 0,
        'badges':     [b.to_dict() for b in badges]
    })

@app.route("/api/tutor/topic-chat/<subject>/<topic_key>")
@login_required
def get_topic_chat(subject, topic_key):
    """Returns the saved conversation for one specific topic, so resuming
    a topic continues where you left off - without pulling in whatever
    a different topic in the same subject was last talking about."""
    if subject not in CURRICULUM:
        return jsonify({"error": "مضمون پیدا نشد"}), 400
    chat = get_or_create_topic_chat(current_user.id, subject, topic_key)
    return jsonify({'chat_history': json.loads(chat.chat_history or '[]')})

@app.route("/api/tutor/start-topic", methods=["POST"])
@login_required
def start_topic():
    """Student starts or resumes a topic."""
    data       = request.get_json()
    subject    = data.get("subject")
    topic_key  = data.get("topic_key")
    level      = data.get("level", 1)

    if subject not in CURRICULUM:
        return jsonify({"error": "مضمون پیدا نشد"}), 400

    progress = get_or_create_progress(current_user.id, subject)

    # find topic info
    topic_info = None
    for t in CURRICULUM[subject]['levels'][level]['topics']:
        if t['key'] == topic_key:
            topic_info = t
            break

    if not topic_info:
        return jsonify({"error": "موضوع پیدا نشد"}), 400

    # update progress
    progress.current_level     = level
    progress.last_topic_title  = topic_info['title']
    progress.last_activity     = datetime.utcnow()
    db.session.commit()

    # award first lesson badge
    badge = award_badge(current_user.id, 'first_lesson', subject)

    return jsonify({
        "topic":   topic_info,
        "badge":   badge,
        "history": json.loads(progress.chat_history or '[]')
    })
@app.route("/api/tutor/chat", methods=["POST"])
@limiter.limit("15 per minute")
def tutor_chat_new():
    data      = request.get_json()
    subject   = data.get("subject")
    topic_key = data.get("topic_key")
    level     = int(data.get("level", 1))
    message   = data.get("message", "")

    if subject not in CURRICULUM:
        return jsonify({"error": "مضمون پیدا نشد"}), 400

    topic_info = None
    for t in CURRICULUM[subject]['levels'].get(level, {}).get('topics', []):
        if t['key'] == topic_key:
            topic_info = t
            break

    if not topic_info:
        return jsonify({"error": "موضوع پیدا نشد"}), 400

    if not current_user.is_authenticated:
        # guest "taste" mode — no persisted progress (nothing to save it
        # against), so the frontend keeps history client-side and resends
        # it each turn, capped at the shared guest daily quota.
        guest_history = data.get("history", [])[-20:]
        system_prompt = build_tutor_system_prompt(
            subject, topic_info['title'], topic_info['desc'], len(guest_history)
        )
        try:
            reply, is_limited, reset_ts = smart_tutor_chat(
                system_prompt=system_prompt,
                history=guest_history,
                user_message=message,
                user_id=None,
                plan='free',
                temperature=0.7
            )
        except Exception as e:
            print(f"Tutor guest chat error: {e}")
            return jsonify({"reply": "متأسفم، مشکلی پیش آمد. لطفاً دوباره امتحان کنید."})

        if is_limited:
            return jsonify({"limited": True, "reset_ts": reset_ts, "guest": True})

        return jsonify({"reply": reply})

    progress = get_or_create_progress(current_user.id, subject)  # subject-level: level/XP/last-studied only
    topic_chat   = get_or_create_topic_chat(current_user.id, subject, topic_key)
    chat_history = json.loads(topic_chat.chat_history or '[]')
    system_prompt = build_tutor_system_prompt(
        subject, topic_info['title'], topic_info['desc'], len(chat_history)
    )
    plan = getattr(current_user, 'tutor_plan', 'free') or 'free'

    try:
        reply, is_limited, reset_ts = smart_tutor_chat(
            system_prompt=system_prompt,
            history=chat_history[-20:],
            user_message=message,
            user_id=current_user.id,
            plan=plan,
            temperature=0.7
        )
    except Exception as e:
        print(f"Tutor chat error: {e}")
        return jsonify({"reply": "متأسفم، مشکلی پیش آمد. لطفاً دوباره امتحان کنید."})

    # hard limit hit — return limit notice, no reply
    if is_limited:
        return jsonify({
            "limited": True,
            "reset_ts": reset_ts
        })

    # save history - scoped to THIS topic only, so switching topics never
    # bleeds into a different conversation
    chat_history.append({"role": "user", "content": message})
    chat_history.append({"role": "assistant", "content": reply})
    if len(chat_history) > 60:
        chat_history = chat_history[-60:]

    topic_chat.chat_history   = json.dumps(chat_history, ensure_ascii=False)
    topic_chat.updated_at     = datetime.utcnow()
    progress.last_activity    = datetime.utcnow()
    progress.last_topic_title = topic_info['title']
    db.session.commit()

    return jsonify({"reply": reply})
@app.route("/api/tutor/complete-topic", methods=["POST"])
@login_required
def complete_topic():
    """Student marks a topic as complete."""
    data      = request.get_json()
    subject   = data.get("subject")
    topic_key = data.get("topic_key")
    level     = int(data.get("level", 1))

    progress = get_or_create_progress(current_user.id, subject)
    completed = json.loads(progress.completed_topics or '[]')

    newly_completed = False
    if topic_key not in completed:
        completed.append(topic_key)
        progress.completed_topics = json.dumps(completed)
        newly_completed = True

        # award XP
        award_xp(current_user.id, XP_REWARDS['topic_complete'], subject)

        # check if entire level is complete
        level_topics = [t['key'] for t in CURRICULUM[subject]['levels'].get(level, {}).get('topics', [])]
        level_done   = all(t in completed for t in level_topics)

        if level_done:
            award_xp(current_user.id, XP_REWARDS['level_complete'], subject)
            badge = award_badge(current_user.id, 'level_complete', subject)
        else:
            badge = None

        db.session.commit()

    user = User.query.get(current_user.id)
    return jsonify({
        "xp_earned":       XP_REWARDS['topic_complete'] if newly_completed else 0,
        "total_xp":        user.total_xp or 0,
        "completed_topics": completed,
        "badge":           None
    })

def run_gated_completion(prompt, max_tokens=2000, temperature=0.8):
    """
    Shared gate for one-shot generation calls (quiz + placement test) that
    used to hit OpenAI directly with NO auth check and NO token tracking —
    meaning anyone, logged in or not, could call them for free all day.
    Now: guests spend from the shared guest daily quota, registered users
    spend from their plan's tutor pool (same tracking smart_tutor_chat uses).
    Returns (raw_text_or_None, is_limited, reset_ts).
    """
    estimated_tokens = max_tokens + len(prompt) // 4

    if not current_user.is_authenticated:
        allowed, ip_hash, date_key = reserve_guest_tokens(estimated_tokens)
        if not allowed:
            return None, True, next_utc_midnight_ts()
        model = GUEST_MODEL
    else:
        plan = getattr(current_user, 'tutor_plan', 'free') or 'free'
        model, reset_ts, is_limited, col = pick_tutor_model(current_user.id, plan, estimated_tokens)
        if is_limited:
            return None, True, reset_ts

    try:
        response = openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=max_tokens,
            temperature=temperature
        )
        raw    = response.choices[0].message.content.strip()
        actual = response.usage.total_tokens
        if not current_user.is_authenticated:
            true_up_guest_tokens(ip_hash, date_key, estimated_tokens, actual)
        else:
            true_up_registered_tutor_tokens(current_user.id, col, estimated_tokens, actual)
        raw = raw.replace('```json', '').replace('```', '').strip()
        return raw, False, None
    except Exception as e:
        print(f"run_gated_completion error: {e}")
        return None, False, None

@app.route("/api/tutor/generate-quiz", methods=["POST"])
@limiter.limit("10 per minute")
def generate_quiz():
    """Generates a fresh quiz for a level using GPT."""
    data    = request.get_json()
    subject = data.get("subject")
    level   = int(data.get("level", 1))

    subject_data = CURRICULUM.get(subject, {})
    level_data   = subject_data.get('levels', {}).get(level, {})
    topics       = level_data.get('topics', [])
    topic_titles = [t['title'] for t in topics]
    subject_name = subject_data.get('name', subject)
    level_title  = level_data.get('title', '')

    quiz_prompt = f"""یک کوییز ۸ سوالی برای این مضمون و سطح بساز:

مضمون: {subject_name}
سطح: {level_title}
موضوعات پوشش داده شده: {', '.join(topic_titles)}

قوانین کوییز:
- ۸ سوال چهار گزینه‌ای
- سوالات متنوع و در سطوح مختلف سختی
- برای هر سوال یک توضیح کوتاه چرا جواب درست است
- سوالات باید واقعاً امتحان کنند که دانش‌آموز چقدر یاد گرفته

فرمت خروجی — فقط JSON خالص بدون هیچ متن دیگری:
{{
  "questions": [
    {{
      "q": "متن سوال به دری",
      "options": ["گزینه الف", "گزینه ب", "گزینه ج", "گزینه د"],
      "correct": 0,
      "explanation": "توضیح چرا این جواب درست است"
    }}
  ]
}}

correct باید index گزینه درست باشد (0، 1، 2 یا 3)."""

    raw, is_limited, reset_ts = run_gated_completion(quiz_prompt, max_tokens=2000, temperature=0.8)

    if is_limited:
        return jsonify({"limited": True, "reset_ts": reset_ts})
    if raw is None:
        return jsonify({"error": "خطا در ساخت کوییز"}), 500

    try:
        return jsonify(json.loads(raw))
    except Exception as e:
        print(f"Quiz JSON parse error: {e}")
        return jsonify({"error": "خطا در ساخت کوییز"}), 500

@app.route("/api/tutor/submit-quiz", methods=["POST"])
@login_required
def submit_quiz():
    """Saves quiz result and awards XP."""
    data      = request.get_json()
    subject   = data.get("subject")
    level     = int(data.get("level", 1))
    score     = int(data.get("score", 0))
    passed    = score >= 70

    quiz_key  = f"{subject}_level{level}"
    xp_earned = 0

    if passed:
        xp_earned = XP_REWARDS['quiz_perfect'] if score == 100 else XP_REWARDS['quiz_pass']
        award_xp(current_user.id, xp_earned, subject)

        if score == 100:
            award_badge(current_user.id, 'quiz_perfect', subject)

        # mark quiz as completed in progress
        progress = get_or_create_progress(current_user.id, subject)
        completed_quizzes = json.loads(progress.completed_quizzes or '[]')
        if quiz_key not in completed_quizzes:
            completed_quizzes.append(quiz_key)
            progress.completed_quizzes = json.dumps(completed_quizzes)
            db.session.commit()

    # save quiz result
    result = QuizResult(
        user_id=current_user.id,
        subject=subject,
        level=level,
        quiz_key=quiz_key,
        score=score,
        passed=passed,
        xp_earned=xp_earned
    )
    db.session.add(result)
    db.session.commit()

    user = User.query.get(current_user.id)
    return jsonify({
        "passed":    passed,
        "score":     score,
        "xp_earned": xp_earned,
        "total_xp":  user.total_xp or 0,
        "message":   "احسنت! سطح بعدی باز شد." if passed else "دوباره امتحان کن — می‌توانی بهتر کنی!"
    })

@app.route("/api/tutor/placement-test", methods=["POST"])
@limiter.limit("10 per minute")
def placement_test():
    """Generates a placement test for a subject."""
    data    = request.get_json()
    subject = data.get("subject")

    subject_data = CURRICULUM.get(subject, {})
    subject_name = subject_data.get('name', subject)

    prompt = f"""یک تست سطح‌بندی ۱۰ سوالی برای {subject_name} بساز.

این تست باید:
- ۲-۳ سوال از سطح ۱ (مبتدی)
- ۲-۳ سوال از سطح ۲ (ابتدایی)
- ۲-۳ سوال از سطح ۳ (متوسط)
- ۲-۳ سوال از سطح ۴ (پیشرفته)

هدف: فهمیدن دانش‌آموز در کدام سطح قرار دارد.

فرمت خروجی — فقط JSON خالص:
{{
  "questions": [
    {{
      "q": "متن سوال به دری",
      "options": ["گزینه الف", "گزینه ب", "گزینه ج", "گزینه د"],
      "correct": 0,
      "level": 1,
      "explanation": "توضیح جواب"
    }}
  ]
}}"""

    raw, is_limited, reset_ts = run_gated_completion(prompt, max_tokens=2000, temperature=0.7)

    if is_limited:
        return jsonify({"limited": True, "reset_ts": reset_ts})
    if raw is None:
        return jsonify({"error": "خطا در ساخت تست"}), 500

    try:
        return jsonify(json.loads(raw))
    except Exception as e:
        print(f"Placement test JSON parse error: {e}")
        return jsonify({"error": "خطا در ساخت تست"}), 500

@app.route("/api/tutor/placement-result", methods=["POST"])

def placement_result():
    """Calculates recommended level from placement test answers."""
    data    = request.get_json()
    subject = data.get("subject")
    answers = data.get("answers", [])  # list of {correct: bool, level: int}

    level_scores = {1: 0, 2: 0, 3: 0, 4: 0}
    level_totals = {1: 0, 2: 0, 3: 0, 4: 0}

    for a in answers:
        lvl = a.get("level", 1)
        level_totals[lvl] = level_totals.get(lvl, 0) + 1
        if a.get("correct"):
            level_scores[lvl] = level_scores.get(lvl, 0) + 1

    # find highest level where student scored 50%+
    recommended = 1
    for lvl in [1, 2, 3, 4]:
        total = level_totals.get(lvl, 0)
        if total > 0:
            pct = level_scores.get(lvl, 0) / total
            if pct >= 0.5:
                recommended = lvl

    return jsonify({
        "recommended_level": recommended,
        "scores": {
            str(lvl): {
                "correct": level_scores.get(lvl, 0),
                "total":   level_totals.get(lvl, 0)
            } for lvl in [1, 2, 3, 4]
        }
    })

if __name__ == "__main__":
    app.run(debug=True)