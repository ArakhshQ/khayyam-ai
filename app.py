from flask import Flask, request, jsonify, render_template, redirect, url_for, send_file
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from database import (db, User, Conversation, Message, Memory, UserTokenUsage, SiteConfig, TutorProgress,
    QuizResult, StudentBadge, CallRequest, PlanEvent)
from functools import wraps
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
from auth import register_user, login_user_by_email, hash_password
from datetime import datetime, timezone, timedelta
import os
import json
import re
import base64
import smtplib
import threading
import hmac
import ssl
import time
import uuid
from email.message import EmailMessage
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from io import BytesIO
import hashlib
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from werkzeug.middleware.proxy_fix import ProxyFix
import router as khr
from metering import (VOICE_PLANS, ensure_storage as ensure_api_meter, reserve as reserve_api,
    finalize as finalize_api, summary as api_meter_summary, chat_plan as active_chat_plan,
    tutor_plan as active_tutor_plan, tutor_tier as active_tutor_tier,
    voice_tier as active_voice_tier, usd_micro, recent_requests)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.getenv("ADMIN_PASSWORD") or os.urandom(32).hex()
if not os.getenv("SECRET_KEY"):
    app.logger.warning("SECRET_KEY is not set: set it on Render (sessions and email links depend on it)")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30),
                  REMEMBER_COOKIE_HTTPONLY=True, REMEMBER_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
                  WTF_CSRF_TIME_LIMIT=None)
database_url = os.getenv("DATABASE_URL", "sqlite:///khayyam.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024
if not database_url.startswith("sqlite"):
    # Supabase drops idle connections: validate on checkout and recycle before it does.
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 280,
                                               'pool_size': 5, 'max_overflow': 5}

# Render terminates TLS in front of us: trust exactly one proxy for scheme and host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=1)

db.init_app(app)
csrf = CSRFProtect(app)

# ---- Client IP behind Render's proxy chain -------------------------------------------
# request.remote_addr is the proxy, so using it would put EVERY guest in one bucket.
# CLIENT_IP_HEADER (e.g. CF-Connecting-IP / True-Client-IP) wins when set; otherwise we take
# the CLIENT_IP_HOPS-th entry from the RIGHT of X-Forwarded-For (entries added by our own
# proxies; anything a client prepends sits further left and is ignored).
# Use /api/admin/ip-debug once after deploying to confirm which setting is right.
CLIENT_IP_HEADER = os.getenv("CLIENT_IP_HEADER", "").strip()
CLIENT_IP_HOPS = int(os.getenv("CLIENT_IP_HOPS", "1"))


def client_ip():
    if CLIENT_IP_HEADER:
        value = request.headers.get(CLIENT_IP_HEADER, "").split(",")[0].strip()
        if value:
            return value[:64]
    parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if parts and 0 < CLIENT_IP_HOPS <= len(parts):
        return parts[-CLIENT_IP_HOPS][:64]
    return (request.remote_addr or "unknown")[:64]


def _rate_key():
    try:
        if current_user.is_authenticated:
            return "u:%s" % current_user.id
    except Exception:
        pass
    return "ip:%s" % client_ip()


limiter = Limiter(key_func=_rate_key, app=app,
                  storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
                  default_limits=["60 per minute", "800 per hour"],
                  default_limits_exempt_when=lambda: request.endpoint == "static")


@app.errorhandler(429)
def rate_limited(_e):
    msg = "تعداد درخواست‌ها زیاد است. کمی صبر کنید و دوباره تلاش کنید."
    if request.path.startswith("/api/"):
        return jsonify(success=False, error=msg, rate_limited=True), 429
    return msg, 429


@app.errorhandler(CSRFError)
def csrf_failed(_e):
    msg = "نشست شما منقضی شده است. صفحه را دوباره بارگذاری کنید."
    if request.path.startswith("/api/"):
        return jsonify(success=False, error=msg, csrf=True), 400
    return msg, 400


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=(), payment=()")
    if app.config["SESSION_COOKIE_SECURE"]:
        h.setdefault("Strict-Transport-Security", "max-age=15552000")
    if request.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    elif resp.mimetype == "text/html":
        h.setdefault("Cache-Control", "private, no-cache")
    return resp


def _once(fn):
    """Run a schema-creation helper once per process (it used to run CREATE TABLE IF NOT
    EXISTS on every request, which is slow and takes locks on Supabase)."""
    done = []
    @wraps(fn)
    def wrapper(*a, **k):
        if done:
            return None
        result = fn(*a, **k)
        done.append(True)
        return result
    return wrapper


def site_url():
    """Base URL used inside emails. Never derived from a client-supplied Host header."""
    configured = os.getenv("SITE_URL", "").strip().rstrip("/")
    return configured or request.host_url.rstrip("/")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'


@login_manager.unauthorized_handler
def _unauthorized():
    if request.path.startswith("/api/"):
        return jsonify(success=False, error="لطفاً ابتدا وارد شوید.", login_required=True), 401
    return redirect(url_for("login_page"))

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=45.0, max_retries=1)
groq_client   = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=30.0, max_retries=1)

KNOWLEDGE_FILE = "knowledge.json"
EXAMPLES_FILE  = "examples.json"

PLAN_CONFIG = {
    'free': [
        {'model': 'gpt-5.4-mini',                              'tier': 1, 'limit': 5000,    'reset': 'daily'},
        {'model': 'gpt-5.4-nano',                              'tier': 2, 'limit': 10000,   'reset': 'daily'},
        {'model': 'openai/gpt-oss-20b', 'tier': 3, 'limit': 35000, 'reset': 'monthly'},
    ],
    'basic': [
        {'model': 'gpt-5.4-mini',                              'tier': 1, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',                              'tier': 2, 'limit': 200000,  'reset': 'monthly'},
        {'model': 'openai/gpt-oss-20b', 'tier': 3, 'limit': 35000, 'reset': 'monthly'},
    ],
    'pro': [
        {'model': 'gpt-5.4-mini',                              'tier': 1, 'limit': 500000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',                              'tier': 2, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'openai/gpt-oss-20b', 'tier': 3, 'limit': 35000, 'reset': 'monthly'},
    ],
    'premium': [
        {'model': 'gpt-5.4',                                   'tier': 1, 'limit': 2000000, 'reset': 'monthly'},
        {'model': 'gpt-5.4-mini',                              'tier': 2, 'limit': 500000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',                              'tier': 3, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'openai/gpt-oss-20b', 'tier': 4, 'limit': 50000, 'reset': 'monthly'},
    ],
}

PLAN_NAMES = {
    'free':    'رایگان',
    'basic':   'پایه — $12',
    'pro':     'حرفه‌ای — $25',
    'premium': 'پریمیوم — $49',
    'tutor_pro': 'استاد حرفه‌ای — $69',
}

with app.app_context():
    db.create_all()
    # Idempotent additive migration, compatible with older `users.plan` deployments.
    from sqlalchemy import inspect
    columns={c['name'] for c in inspect(db.engine).get_columns('users')}
    with db.engine.begin() as conn:
        additions={
          'is_verified':'BOOLEAN DEFAULT FALSE',
          'email_verified':'BOOLEAN DEFAULT FALSE',
          'chat_plan':"VARCHAR(20) DEFAULT 'free'",
          'chat_plan_expires_at':'TIMESTAMP',
          'tutor_plan':"VARCHAR(20) DEFAULT 'free'",
          'tutor_plan_expires_at':'TIMESTAMP',
        }
        for name,typ in additions.items():
            if name not in columns:
                conn.execute(sql_text(f'ALTER TABLE users ADD COLUMN {name} {typ}'))
        # Old DB's `plan` was the only subscription record; preserve it verbatim.
        if 'plan' in columns and 'chat_plan' not in columns:
            conn.execute(sql_text("UPDATE users SET chat_plan=COALESCE(plan,'free')"))
        # One verified flag: anyone verified under the older `is_verified` stays verified.
        conn.execute(sql_text("UPDATE users SET email_verified=TRUE WHERE is_verified=TRUE "
                              "AND (email_verified IS NULL OR email_verified=FALSE)"))
        conn.execute(sql_text("UPDATE users SET email_verified=FALSE WHERE email_verified IS NULL"))
        conn.execute(sql_text("UPDATE users SET chat_plan='free' WHERE chat_plan IS NULL"))
        conn.execute(sql_text("UPDATE users SET tutor_plan='free' WHERE tutor_plan IS NULL"))
    ensure_api_meter(db)

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
    # Legacy compatibility (actual dispatch goes through metered_smart_chat).
    # Never pre-charge legacy counters or provide an uncapped fallback.
    usage = get_or_create_usage(user_id)
    if usage is None:
        return None, None, None, True
    for item in PLAN_CONFIG.get(plan, PLAN_CONFIG['free']):
        col = f"tier{item['tier']}_tokens"
        resetcol = f"tier{item['tier']}_reset"
        if not hasattr(usage, col):
            continue
        ts = getattr(usage, resetcol) or datetime.utcnow()
        used = (getattr(usage, col) or 0) if not should_reset(ts, item['reset']) else 0
        if used + tokens_to_use <= item['limit']:
            return item['model'], item['tier'], get_reset_timestamp(ts,item['reset']), False
    return None,None,None,True

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

def pick_tutor_model(user_id, plan, tokens_to_use):
    # Compatibility helper. Dispatch and charging happen in smart_tutor_chat.
    for item in TUTOR_PLAN_CONFIG.get(plan,TUTOR_PLAN_CONFIG['free']):
        return item['model'], None, False
    return None,None,True

def _usage_pair(response):
    """Only trust provider-reported billable counts, not guessed tokens."""
    usage = getattr(response, 'usage', None)
    if usage is None:
        return None
    inp = getattr(usage, 'prompt_tokens', None)
    out = getattr(usage, 'completion_tokens', None)
    if inp is None or out is None:
        return None
    return (max(0, int(inp)), max(0, int(out)))


def _maybe_billed(exc):
    """Did the provider possibly bill a call that raised? HTTP error replies (4xx/5xx)
    are not billed, so they are refunded. A timeout may have been generated, so it is
    charged (conservatively)."""
    name = type(exc).__name__
    return name in ('APITimeoutError', 'ReadTimeout', 'TimeoutException', 'Timeout')


def _next_reset_ts(options):
    """Earliest moment any tier's window rolls over (UTC unix seconds)."""
    from metering import next_reset_ts
    periods = {o['reset'] for o in options if o.get('reset')}
    return min((next_reset_ts(p) for p in periods), default=None)


def _attempt(grant, system_prompt, messages, temperature, image_b64, image_type, effort, json_mode):
    """One provider call against one reservation. ALWAYS settles the reservation.
    Returns the reply text; raises on failure (after refunding or charging correctly)."""
    t0 = time.monotonic()
    ms = lambda: int((time.monotonic() - t0) * 1000)
    model = grant['model']
    try:
        if model.startswith('openai/gpt-oss'):
            reply, response = call_groq_model(system_prompt, messages, temperature,
                                              model=model, return_response=True)
        else:
            reply, response = call_openai_model(model, system_prompt, messages, temperature,
                                                image_b64, image_type, return_response=True,
                                                max_output=grant['max_output'],
                                                effort=effort, json_mode=json_mode)
    except Exception as exc:
        try:
            finalize_api(db, grant, uncertain=_maybe_billed(exc), failed=not _maybe_billed(exc),
                         latency_ms=ms())
        except Exception:
            db.session.rollback()
            app.logger.exception('Could not settle reservation after provider error')
        raise
    usage = _usage_pair(response)
    try:
        if usage is None:
            finalize_api(db, grant, uncertain=True, latency_ms=ms())
        else:
            finalize_api(db, grant, usage[0], usage[1], latency_ms=ms())
    except Exception:
        db.session.rollback()
        app.logger.exception('Could not settle reservation after provider reply')
    app.logger.info('ai_call feature=%s model=%s intent=%s in=%s out=%s ms=%s',
                    grant['feature'], model, grant.get('intent'),
                    usage[0] if usage else '?', usage[1] if usage else '?', ms())
    if not reply or not reply.strip():
        raise RuntimeError('Provider returned no visible answer')
    return reply


def _metered_completion(feature, system_prompt, messages, user_id, plan,
                        temperature=.7, image_b64=None, image_type=None,
                        max_output=1000, level='standard', effort=None,
                        json_mode=False, intent_label=None):
    """Route -> reserve -> call -> settle. Returns (reply, switched, reset_ts, model).

    reply None + model 'limited' : the user's allowance (or the guest budget) is used up
    reply None + model 'error'   : every affordable provider failed (nothing was charged)
    """
    from metering import reserve, guest_global_spent_micro
    guest_key = (hashlib.sha256((app.secret_key + ':' + client_ip()).encode()).hexdigest()
                 if user_id is None else None)
    config = PLAN_CONFIG.get(plan, PLAN_CONFIG['free']) if feature == 'chat' \
        else TUTOR_PLAN_CONFIG.get(plan, TUTOR_PLAN_CONFIG['free'])
    options = [o for o in khr.order_options(config, level)
               if not (image_b64 and o['model'].startswith('openai/gpt-oss'))]

    # Token estimate without a network round trip. `overhead` is Khayyam's own fixed
    # system prompt: it counts toward OUR dollar guard but never toward the user's window.
    overhead = khr.estimate_tokens(system_prompt) + 15
    body = sum(khr.estimate_tokens(m.get('content', '')) + 10 for m in messages) + (2500 if image_b64 else 0)
    total_chars = len(system_prompt) + sum(len(str(m.get('content', ''))) for m in messages)
    if total_chars > 60000:
        raise ValueError('Message history is too long')
    estimated = overhead + body

    if user_id is None:
        cap = float(os.getenv('GUEST_GLOBAL_DAILY_USD', '3.0'))
        if guest_global_spent_micro(db) >= cap * 1e6:
            return None, True, _next_reset_ts([{'reset': 'daily'}]), 'limited'

    first_choice = None
    attempts = 0
    while options and attempts < 3:
        attempts += 1
        grant, idx = reserve(db, user_id, feature, plan, options, estimated, max_output,
                             guest_key=guest_key, overhead_tokens=overhead, intent=intent_label or level)
        if not grant:
            if first_choice is None:
                return None, True, _next_reset_ts(options), 'limited'
            break
        if first_choice is None:
            first_choice = options[0]['model']
        grant['sys_share'] = len(system_prompt) / max(1, total_chars)
        try:
            reply = _attempt(grant, system_prompt, messages, temperature, image_b64, image_type,
                             effort, json_mode)
            return reply, (grant['model'] != first_choice), None, grant['model']
        except Exception:
            app.logger.exception('Provider call failed (%s); trying the next affordable model', grant['model'])
            options = options[idx + 1:]
    return None, True, None, 'error'


def smart_tutor_chat(system_prompt, history, user_message, user_id=None, plan='free', temperature=0.7):
    messages = khr.trim_history(history, tutor=True) + [{'role': 'user', 'content': user_message}]
    reply, switched, reset_ts, model = _metered_completion('tutor', system_prompt, messages, user_id,
        plan, temperature, max_output=900, level='standard', intent_label='tutor')
    if reply is None and model == 'error':
        raise RuntimeError('All tutor providers failed')
    return reply, (reply is None), reset_ts

# ── KNOWLEDGE (DATABASE BACKED) ──
_KB_CACHE = {}
_KB_TTL = 60  # seconds; admin edits show up on every worker within a minute


def _kb_cached(name, loader):
    hit = _KB_CACHE.get(name)
    now = time.monotonic()
    if hit and now - hit[0] < _KB_TTL:
        return hit[1]
    data = loader()
    _KB_CACHE[name] = (now, data)
    return data


def load_knowledge():
    return _kb_cached('knowledge', _load_knowledge_db)


def load_examples():
    return _kb_cached('examples', _load_examples_db)


def _load_knowledge_db():
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

def _load_examples_db():
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
        _KB_CACHE.pop('knowledge', None)
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
        _KB_CACHE.pop('examples', None)
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
    """Save an explicit "remember this" request. Rule-based: no model call, no tokens,
    no extra latency (the old version made a second paid call per request)."""
    memory_text = khr.extract_memory_text(user_message)
    if not memory_text:
        return
    try:
        if not Memory.query.filter_by(user_id=user_id, content=memory_text).first():
            # Keep the memory list bounded so prompts never grow without limit.
            if Memory.query.filter_by(user_id=user_id).count() >= 50:
                oldest = Memory.query.filter_by(user_id=user_id).order_by(Memory.created_at.asc()).first()
                if oldest: db.session.delete(oldest)
            db.session.add(Memory(user_id=user_id, content=memory_text))
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception('Memory save failed')

# ── PROMPTS ──
def build_system_prompt(user_memories=None, message='', kind='chat', level='standard'):
    """Compact core prompt + only the knowledge relevant to this message (see router.py)."""
    return khr.build_chat_prompt(load_knowledge(), load_examples(), user_memories, message, kind, level)

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
def call_openai_model(model, system_prompt, messages, temperature=.7,
                      image_b64=None, image_type=None, return_response=False,
                      max_output=1000, effort=None, json_mode=False):
    formatted=[]
    for m in messages:
        if m.get('role')=='user' and image_b64 and m is messages[-1]:
            formatted.append({'role':'user','content':[
              {'type':'text','text':m['content']},
              {'type':'image_url','image_url':{'url':f'data:{image_type};base64,{image_b64}'}}]})
        else: formatted.append(m)
    is_reasoning=any(x in model for x in ('gpt-5','o1','o3','o4'))
    kwargs={'model':model,'messages':[{'role':'system','content':system_prompt}]+formatted,
            'max_completion_tokens' if is_reasoning else 'max_tokens':max_output}
    if not is_reasoning:kwargs['temperature']=temperature
    if json_mode:kwargs['response_format']={'type':'json_object'}
    if effort and is_reasoning:kwargs['reasoning_effort']=effort
    try:
        response=openai_client.chat.completions.create(**kwargs)
    except Exception as exc:
        # An unsupported optional parameter must never take the whole answer down.
        text=str(exc).lower()
        if type(exc).__name__=='BadRequestError' and ('reasoning_effort' in text or 'response_format' in text):
            kwargs.pop('reasoning_effort',None); kwargs.pop('response_format',None)
            response=openai_client.chat.completions.create(**kwargs)
        else:
            raise
    reply=response.choices[0].message.content or ''
    return (reply,response) if return_response else (reply,getattr(response.usage,'total_tokens',0))


def call_groq_model(system_prompt, messages, temperature=.7,
                    model='openai/gpt-oss-20b',return_response=False):
    if model not in ('openai/gpt-oss-20b','openai/gpt-oss-120b'):
        raise ValueError('Unsupported fallback model')
    response=groq_client.chat.completions.create(model=model,
        messages=[{'role':'system','content':system_prompt}]+messages,
        temperature=temperature,max_tokens=650)
    reply=response.choices[0].message.content or ''
    return (reply,response) if return_response else (reply,getattr(response.usage,'total_tokens',0))


def smart_chat(system_prompt, history, user_message, user_id=None, plan='free',
               temperature=.7, image_b64=None, image_type=None, intent=None):
    """intent: dict from router.classify (level/kind/max_output/effort). Defaults to standard."""
    intent = intent or {'level': 'standard', 'kind': 'chat', 'max_output': 900, 'effort': None}
    messages=history+[{'role':'user','content':user_message}]
    reply,switched,reset_ts,model = _metered_completion('chat',system_prompt,messages,
        user_id,plan,temperature,image_b64,image_type,max_output=intent['max_output'],
        level=intent['level'],effort=intent.get('effort'),intent_label=intent.get('kind'))
    if reply is None:
        if model == 'error':
            return 'سرویس موقتاً در دسترس نیست. چیزی از سهمیه شما کم نشد؛ لطفاً چند لحظه بعد دوباره تلاش کنید.', False, None, 'error'
        return 'محدودیت استفاده تکمیل شده است. بعداً دوباره تلاش کنید.', True, reset_ts, 'limited'
    return reply,switched,reset_ts,model

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
        return redirect(url_for('chat_page') if current_user.email_verified else url_for('check_email_page'))
    return render_template("register.html")

@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('chat_page') if current_user.email_verified else url_for('check_email_page'))
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
@limiter.limit("5 per hour")
def api_register():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 150:
        return jsonify(success=False, error="آدرس ایمیل معتبر وارد کنید."), 400
    if not re.fullmatch(r"[\w.\-]{3,40}", username):
        return jsonify(success=False, error="نام کاربری باید ۳ تا ۴۰ نویسه و فقط شامل حروف، عدد، نقطه، خط تیره یا زیرخط باشد."), 400
    if not 8 <= len(password) <= 128:
        return jsonify(success=False, error="رمز عبور باید بین ۸ و ۱۲۸ نویسه باشد."), 400

    try:
        user, error = register_user(username, password, email, None)
    except IntegrityError:
        db.session.rollback()
        return jsonify(success=False, error="این نام کاربری یا ایمیل قبلاً ثبت شده است."), 400
    if error:
        return jsonify(success=False, error=error), 400

    user.email_verified = False
    db.session.commit()
    try:
        sent = send_account_email(user, "verify")
    except Exception:
        app.logger.exception("Verification email delivery failed")
        sent = False

    return jsonify(success=True, verification_required=True,
                   verification_email_sent=sent, redirect="/check-email")


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute;40 per hour")
def api_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or data.get("identifier") or "").strip().lower()
    password = str(data.get("password") or "")
    user, error = login_user_by_email(email, password)
    if error or user is None:
        return jsonify(success=False, error="ایمیل یا رمز عبور نادرست است."), 401
    if not user.email_verified:
        return jsonify(success=False, verification_required=True,
                       error="لطفاً ابتدا ایمیل خود را تأیید کنید."), 403
    login_user(user)
    return jsonify(success=True)


@app.route("/check-email")
def check_email_page():
    return render_template("check_email.html")


@app.before_request
def require_verified_email():
    if not current_user.is_authenticated or current_user.email_verified:
        return None
    # Keep recovery, verification, logout, and static files accessible.
    allowed = {
        "static", "home", "login_page", "register_page", "logout",
        "check_email_page", "verify_email", "resend_verification",
        "forgot_password_page", "reset_password_page",
        "api_forgot_password", "api_reset_password",
        "api_login", "api_register",
    }
    if request.endpoint in allowed:
        return None
    if request.path.startswith("/api/"):
        return jsonify(success=False, verification_required=True,
                       error="لطفاً ابتدا ایمیل خود را تأیید کنید."), 403
    return redirect(url_for("check_email_page"))


# Signed, expiring tokens. Password-reset tokens become invalid after password changes.
def account_token(user, purpose):
    payload = {'uid': user.id, 'purpose': purpose, 'email': user.email}
    if purpose == 'reset':
        payload['password_hash'] = user.password_hash
    return URLSafeTimedSerializer(app.secret_key).dumps(payload, salt='khayyam-account-v1')

def token_user(token, purpose, max_age):
    try:
        data = URLSafeTimedSerializer(app.secret_key).loads(token, salt='khayyam-account-v1', max_age=max_age)
        if data.get('purpose') != purpose: return None
        user = db.session.get(User, data.get('uid'))
        if not user or user.email != data.get('email'): return None
        if purpose == 'reset' and user.password_hash != data.get('password_hash'): return None
        return user
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None

def send_account_email(user, purpose):
    host = os.getenv('SMTP_HOST', '').strip()
    port = int(os.getenv('SMTP_PORT', '587'))
    sender = os.getenv('SMTP_FROM', '').strip()
    username = os.getenv('SMTP_USER', '').strip()
    password = os.getenv('SMTP_PASSWORD', '')
    if not host or not sender or not username or not password:
        app.logger.warning('Brevo SMTP is not configured; email was not sent')
        return False
    token = account_token(user, purpose)
    if purpose == 'verify':
        link = site_url() + url_for('verify_email', token=token)
        subject = 'تأیید ایمیل خیام | Verify your Khayyam email'
        body = f'برای تأیید ایمیل خود روی لینک زیر کلیک کنید (اعتبار: ۲۴ ساعت):\n{link}\n\nIf you did not register, ignore this email.'
    else:
        link = site_url() + url_for('reset_password_page', token=token)
        subject = 'بازیابی رمز عبور خیام | Reset your Khayyam password'
        body = f'برای تعیین رمز عبور جدید روی لینک زیر کلیک کنید (اعتبار: ۳۰ دقیقه):\n{link}\n\nIf you did not request this, ignore this email.'
    message = EmailMessage()
    message['From'] = sender
    message['To'] = user.email
    message['Subject'] = subject
    message.set_content(body)
    from html import escape
    heading = "تأیید آدرس ایمیل" if purpose == "verify" else "بازیابی رمز عبور"
    description = ("برای فعال‌سازی حساب خیام، روی دکمه زیر کلیک کنید."
                   if purpose == "verify" else
                   "برای انتخاب رمز عبور جدید، روی دکمه زیر کلیک کنید.")
    button_label = "تأیید ایمیل" if purpose == "verify" else "تعیین رمز عبور جدید"
    validity = "۲۴ ساعت" if purpose == "verify" else "۳۰ دقیقه"
    safe_link = escape(link, quote=True)
    message.add_alternative(f"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:32px 14px;background:#0f0e0c;color:#ede8df;font-family:Tahoma,Arial,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;max-width:520px;margin:auto;background:#1a1814;border:1px solid #2e2a22;border-radius:16px">
<tr><td style="padding:36px 28px;text-align:center">
<div style="color:#c9a84c;font-size:29px;font-weight:bold;margin-bottom:22px">خیام</div>
<h1 style="font-size:22px;color:#ede8df">{heading}</h1>
<p style="color:#b5ab99;font-size:14px;line-height:2;margin-bottom:28px">{description}</p>
<a href="{safe_link}" style="display:inline-block;background:#c9a84c;color:#0f0e0c;padding:14px 28px;border-radius:9px;text-decoration:none;font-weight:bold">{button_label}</a>
<p style="color:#8b8170;font-size:12px;line-height:2;margin-top:28px">اعتبار لینک: {validity}</p>
<p style="color:#8b8170;font-size:12px;line-height:2">اگر این درخواست را انجام نداده‌اید، این ایمیل را نادیده بگیرید.</p>
<p style="color:#8b8170;font-size:12px;border-top:1px solid #2e2a22;padding-top:20px">Khayyam AI</p>
</td></tr></table></body></html>""", subtype="html")
    _smtp_send(message)
    return True

def _smtp_configured():
    return all(os.getenv(k, '').strip() for k in ('SMTP_HOST', 'SMTP_FROM', 'SMTP_USER')) and bool(os.getenv('SMTP_PASSWORD'))


def _smtp_send(message):
    host = os.getenv('SMTP_HOST', '').strip()
    port = int(os.getenv('SMTP_PORT', '587'))
    username = os.getenv('SMTP_USER', '').strip()
    password = os.getenv('SMTP_PASSWORD', '')
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


def send_card_email(to_address, subject, heading, description, lines=(), button_label=None, link=None):
    """Branded transactional email (same look as the verification email)."""
    if not _smtp_configured():
        app.logger.warning('SMTP is not configured; email "%s" was not sent', subject)
        return False
    from html import escape
    message = EmailMessage()
    message['From'] = os.getenv('SMTP_FROM', '').strip()
    message['To'] = to_address
    message['Subject'] = subject
    plain = [heading, '', description] + [''] + list(lines) + ([''] + [link] if link else [])
    message.set_content('\n'.join(plain))
    rows = ''.join(f'<p style="color:#ede8df;font-size:14px;line-height:2;margin:4px 0">{escape(x)}</p>' for x in lines)
    button = (f'<a href="{escape(link, quote=True)}" style="display:inline-block;background:#c9a84c;color:#0f0e0c;'
              f'padding:14px 28px;border-radius:9px;text-decoration:none;font-weight:bold;margin-top:22px">'
              f'{escape(button_label or "")}</a>') if link and button_label else ''
    message.add_alternative(f"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:32px 14px;background:#0f0e0c;color:#ede8df;font-family:Tahoma,Arial,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;max-width:520px;margin:auto;background:#1a1814;border:1px solid #2e2a22;border-radius:16px">
<tr><td style="padding:36px 28px;text-align:center">
<div style="color:#c9a84c;font-size:29px;font-weight:bold;margin-bottom:22px">خیام</div>
<h1 style="font-size:22px;color:#ede8df">{escape(heading)}</h1>
<p style="color:#b5ab99;font-size:14px;line-height:2;margin-bottom:20px">{escape(description)}</p>
{rows}{button}
<p style="color:#8b8170;font-size:12px;border-top:1px solid #2e2a22;padding-top:20px;margin-top:28px">Khayyam AI</p>
</td></tr></table></body></html>""", subtype='html')
    _smtp_send(message)
    return True


def _run_async(fn, *args):
    """Fire-and-forget (email must never slow down or break a request)."""
    def runner():
        try:
            with app.app_context():
                fn(*args)
        except Exception:
            app.logger.exception('Background task failed')
    threading.Thread(target=runner, daemon=True).start()


# Lightweight per-process throttling; production multi-worker deployments should use Redis.
_email_requests = {}
def email_throttled(email, action):
    key = (email, action)
    now = time.monotonic()
    if now - _email_requests.get(key, 0) < 60: return True
    _email_requests[key] = now
    if len(_email_requests) > 5000:
        for k, t in list(_email_requests.items()):
            if now - t > 3600: _email_requests.pop(k, None)
    return False

# ══════════════════════════════════════════
# ── PLANS: grants, expiry, reminders, call-us requests ──
# ══════════════════════════════════════════
PLAN_PERIOD_DAYS = 30
CHAT_PLANS = ('free', 'basic', 'pro', 'premium')
TUTOR_PLANS = ('free', 'tutor_pro')


def plan_label(plan):
    """'پایه — $12' -> 'پایه'."""
    return PLAN_NAMES.get(plan, plan).split(' — ')[0]


def _plan_fields(kind):
    return ('chat_plan', 'chat_plan_expires_at') if kind == 'chat' else ('tutor_plan', 'tutor_plan_expires_at')


def _fmt_date(dt):
    return dt.strftime('%Y-%m-%d') if dt else ''


def _send_plan_email(user_id, event, kind, plan, expires_iso):
    user = db.session.get(User, user_id)
    if not user or not user.email:
        return
    expires = datetime.fromisoformat(expires_iso) if expires_iso else None
    area = 'گفتگو' if kind == 'chat' else 'استاد'
    name = f'{plan_label(plan)} ({area})'
    pricing = site_url() + '/pricing'
    if event == 'granted':
        send_card_email(user.email, 'پلان شما فعال شد | Your Khayyam plan is active',
            'پلان شما فعال شد', f'پلان {name} برای حساب شما فعال شد. از همراهی شما سپاسگزاریم.',
            [f'اعتبار تا: {_fmt_date(expires)}'], 'رفتن به خیام', site_url() + '/chat')
    elif event == 'expired':
        send_card_email(user.email, 'پلان شما به پایان رسید | Your Khayyam plan has ended',
            'پلان شما به پایان رسید',
            f'اعتبار پلان {name} تمام شد و حساب شما به پلان رایگان برگشت. گفتگوهای شما همچنان ذخیره است.',
            [], 'تمدید پلان', pricing)
    elif event == 'warned':
        send_card_email(user.email, 'پلان شما به‌زودی تمام می‌شود | Your Khayyam plan ends soon',
            'پلان شما به‌زودی تمام می‌شود',
            f'اعتبار پلان {name} در تاریخ {_fmt_date(expires)} تمام می‌شود. برای ادامه، با ما تماس بگیرید.',
            [], 'تمدید پلان', pricing)
    elif event == 'revoked':
        send_card_email(user.email, 'پلان شما تغییر کرد | Your Khayyam plan changed',
            'پلان شما تغییر کرد', f'پلان {name} از حساب شما برداشته شد.', [], 'مشاهده پلان‌ها', pricing)


def grant_plan(user, kind, plan, months=1, actor='system', expires_at=None, notify=True):
    """Grant (or revoke with plan='free') a plan. Monthly and stackable:
    renewing the SAME active plan extends from its current expiry; switching plans starts a
    fresh period now. Chat and Tutor plans are independent of each other."""
    if kind not in ('chat', 'tutor'):
        raise ValueError('Invalid plan kind')
    allowed = CHAT_PLANS if kind == 'chat' else TUTOR_PLANS
    if plan not in allowed:
        raise ValueError('Invalid plan for this kind')
    months = max(1, min(int(months or 1), 24))
    plan_col, exp_col = _plan_fields(kind)
    now = datetime.utcnow()
    if plan == 'free':
        setattr(user, plan_col, 'free'); setattr(user, exp_col, None)
        new_exp = None; event = 'revoked'
    else:
        current_plan = getattr(user, plan_col) or 'free'
        current_exp = getattr(user, exp_col)
        if expires_at is not None:
            new_exp = expires_at
        else:
            base = current_exp if (current_plan == plan and current_exp and current_exp > now) else now
            new_exp = base + timedelta(days=PLAN_PERIOD_DAYS * months)
        setattr(user, plan_col, plan); setattr(user, exp_col, new_exp)
        event = 'granted'
    db.session.add(PlanEvent(user_id=user.id, plan_kind=kind, plan=plan, event=event,
                             expires_at=new_exp, actor=actor))
    db.session.commit()
    if notify:
        _run_async(_send_plan_email, user.id, event, kind, plan, new_exp.isoformat() if new_exp else None)
    return new_exp


def expire_plans_for(user, notify=True):
    """Revert any lapsed plan on `user` to free (and tell them). Cheap: only touches the DB
    when something actually expired. Returns the list of kinds that expired."""
    now = datetime.utcnow()
    expired = []
    for kind in ('chat', 'tutor'):
        plan_col, exp_col = _plan_fields(kind)
        plan, exp = getattr(user, plan_col), getattr(user, exp_col)
        if plan and plan != 'free' and exp is not None and exp <= now:
            db.session.add(PlanEvent(user_id=user.id, plan_kind=kind, plan=plan, event='expired',
                                     expires_at=exp, actor='system'))
            setattr(user, plan_col, 'free'); setattr(user, exp_col, None)
            expired.append((kind, plan, exp))
    if expired:
        db.session.commit()
        if notify:
            for kind, plan, exp in expired:
                _run_async(_send_plan_email, user.id, 'expired', kind, plan, exp.isoformat())
    return [k for k, _, _ in expired]


def sweep_plans(warn_days=3):
    """Expire lapsed plans for everyone and send one reminder per plan period.
    Safe to run as often as you like."""
    now = datetime.utcnow()
    expired_count = warned_count = 0
    for kind in ('chat', 'tutor'):
        plan_col, exp_col = _plan_fields(kind)
        plan_attr, exp_attr = getattr(User, plan_col), getattr(User, exp_col)
        lapsed = User.query.filter(plan_attr != 'free', exp_attr.isnot(None), exp_attr <= now).all()
        for user in lapsed:
            expired_count += len(expire_plans_for(user, notify=True))
        soon = User.query.filter(plan_attr != 'free', exp_attr.isnot(None), exp_attr > now,
                                 exp_attr <= now + timedelta(days=warn_days)).all()
        for user in soon:
            plan, exp = getattr(user, plan_col), getattr(user, exp_col)
            already = PlanEvent.query.filter_by(user_id=user.id, plan_kind=kind, event='warned',
                                                expires_at=exp).first()
            if already:
                continue
            db.session.add(PlanEvent(user_id=user.id, plan_kind=kind, plan=plan, event='warned',
                                     expires_at=exp, actor='system'))
            db.session.commit()
            _run_async(_send_plan_email, user.id, 'warned', kind, plan, exp.isoformat())
            warned_count += 1
    return expired_count, warned_count


@app.before_request
def lazy_plan_expiry():
    """Every logged-in request checks the (already loaded) user's expiry dates in memory."""
    if request.endpoint == 'static' or not current_user.is_authenticated:
        return None
    try:
        expire_plans_for(current_user)
    except Exception:
        db.session.rollback()
        app.logger.exception('Plan expiry check failed')
    return None


@app.route('/internal/plans/sweep', methods=['POST', 'GET'])
@csrf.exempt
def internal_plan_sweep():
    """For a Render Cron Job: curl -X POST -H "X-Cron-Secret: $CRON_SECRET" <site>/internal/plans/sweep"""
    secret = os.getenv('CRON_SECRET', '')
    supplied = request.headers.get('X-Cron-Secret', '')
    if not secret or not hmac.compare_digest(secret.encode(), supplied.encode()):
        return jsonify(error='forbidden'), 403
    expired, warned = sweep_plans()
    return jsonify(success=True, expired=expired, warned=warned)


PHONE_RE = re.compile(r'^\+?[0-9][0-9\s\-]{6,19}$')


@app.route('/api/call-request', methods=['POST'])
@limiter.limit('5 per hour')
def api_call_request():
    """"Call me to buy a plan" form on the pricing page (works for guests and users)."""
    data = request.get_json(silent=True) or {}
    name = ' '.join(str(data.get('name') or '').split())[:120]
    phone = ' '.join(str(data.get('phone') or '').split())
    plan = str(data.get('plan') or '')
    kind = 'tutor' if plan in TUTOR_PLANS and plan != 'free' else 'chat'
    note = str(data.get('note') or '').strip()[:500]
    try:
        months = max(1, min(int(data.get('months') or 1), 12))
    except (TypeError, ValueError):
        months = 1
    if len(name) < 2:
        return jsonify(success=False, error='نام خود را وارد کنید.'), 400
    if not PHONE_RE.match(phone):
        return jsonify(success=False, error='شماره تماس معتبر وارد کنید (مثال: +93700000000).'), 400
    if plan == 'free' or plan not in (set(CHAT_PLANS) | set(TUTOR_PLANS)):
        return jsonify(success=False, error='پلان نامعتبر است.'), 400
    # A person can have only a few open requests at once.
    open_filter = [CallRequest.status.in_(('new', 'contacted'))]
    if current_user.is_authenticated:
        open_filter.append(CallRequest.user_id == current_user.id)
    else:
        open_filter.append(CallRequest.phone == phone)
    if CallRequest.query.filter(*open_filter).count() >= 3:
        return jsonify(success=False, error='درخواست‌های قبلی شما در حال بررسی است. به‌زودی با شما تماس می‌گیریم.'), 429
    req = CallRequest(user_id=current_user.id if current_user.is_authenticated else None,
                      name=name, phone=phone[:40], plan_kind=kind, plan=plan, months=months, note=note or None)
    db.session.add(req)
    db.session.commit()
    notify_to = os.getenv('ADMIN_NOTIFY_EMAIL', '').strip()
    if notify_to:
        _run_async(lambda: send_card_email(notify_to, 'درخواست خرید جدید | New plan request',
            'درخواست خرید جدید', f'{name} ({phone}) پلان {plan_label(plan)} را برای {months} ماه درخواست کرده است.',
            [note] if note else []))
    return jsonify(success=True, message='درخواست شما ثبت شد. به‌زودی با شما تماس می‌گیریم.')


@app.route('/api/admin/call-requests', methods=['GET'])
@login_required
@admin_required
def admin_call_requests():
    status = request.args.get('status', '')
    query = CallRequest.query
    if status in ('new', 'contacted', 'paid', 'rejected'):
        query = query.filter_by(status=status)
    rows = query.order_by(CallRequest.created_at.desc()).limit(200).all()
    users = {u.id: u for u in User.query.filter(User.id.in_([r.user_id for r in rows if r.user_id])).all()} if rows else {}
    out = []
    for r in rows:
        d = r.to_dict()
        u = users.get(r.user_id)
        d['username'] = u.username if u else None
        d['email'] = u.email if u else None
        d['plan_label'] = plan_label(r.plan)
        out.append(d)
    return jsonify(out)


@app.route('/api/admin/call-requests/<int:req_id>', methods=['POST'])
@login_required
@admin_required
def admin_update_call_request(req_id):
    req = db.session.get(CallRequest, req_id)
    if not req:
        return jsonify(success=False, error='not found'), 404
    data = request.get_json(silent=True) or {}
    status = str(data.get('status') or req.status)
    if status not in ('new', 'contacted', 'paid', 'rejected'):
        return jsonify(success=False, error='Invalid status'), 400
    req.status = status
    if 'admin_note' in data:
        req.admin_note = str(data.get('admin_note') or '')[:500] or None
    req.handled_at = datetime.utcnow() if status != 'new' else None
    db.session.commit()
    return jsonify(success=True)


@app.route('/api/admin/call-requests/<int:req_id>/grant', methods=['POST'])
@login_required
@admin_required
def admin_grant_from_request(req_id):
    """After the customer has paid by phone: activate the plan and email them."""
    req = db.session.get(CallRequest, req_id)
    if not req:
        return jsonify(success=False, error='not found'), 404
    data = request.get_json(silent=True) or {}
    user = db.session.get(User, req.user_id) if req.user_id else None
    lookup = str(data.get('user') or '').strip().lower()
    if lookup:   # request came from a guest: admin names the account (email or username)
        user = User.query.filter((User.email == lookup) | (User.username == lookup)).first()
    if not user:
        return jsonify(success=False, error='این درخواست حساب کاربری ندارد؛ ایمیل یا نام کاربری را وارد کنید.'), 400
    try:
        months = int(data.get('months') or req.months or 1)
        new_exp = grant_plan(user, req.plan_kind, req.plan, months=months, actor=current_user.username)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    req.user_id = user.id
    req.status = 'paid'
    req.handled_at = datetime.utcnow()
    db.session.commit()
    return jsonify(success=True, expires_at=new_exp.isoformat() if new_exp else None)


@app.route('/api/admin/sweep-plans', methods=['POST'])
@login_required
@admin_required
def admin_sweep_plans():
    expired, warned = sweep_plans()
    return jsonify(success=True, expired=expired, warned=warned)


@app.route('/api/admin/ip-debug')
@login_required
@admin_required
def admin_ip_debug():
    """Open once on the live site to confirm which CLIENT_IP_* setting identifies visitors."""
    return jsonify(resolved=client_ip(), remote_addr=request.remote_addr,
                   x_forwarded_for=request.headers.get('X-Forwarded-For'),
                   cf_connecting_ip=request.headers.get('CF-Connecting-IP'),
                   true_client_ip=request.headers.get('True-Client-IP'),
                   settings={'CLIENT_IP_HEADER': CLIENT_IP_HEADER, 'CLIENT_IP_HOPS': CLIENT_IP_HOPS})


@app.route('/api/admin/ai-log')
@login_required
@admin_required
def admin_ai_log():
    """Newest AI requests: which model/intent, what the user was charged, cost, latency."""
    return jsonify(recent_requests(db, 80))


@app.route('/forgot-password')
def forgot_password_page():
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>')
def reset_password_page(token):
    return render_template('reset_password.html', token=token, valid=bool(token_user(token, 'reset', 1800)))

@app.route('/api/forgot-password', methods=['POST'])
@limiter.limit("5 per hour")
def api_forgot_password():
    email = str((request.get_json(silent=True) or {}).get('email', '')).strip().lower()
    if not email or len(email) > 150: return jsonify({'error':'ایمیل معتبر وارد کنید'}), 400
    user = User.query.filter_by(email=email).first()
    if user and not email_throttled(email, 'reset'):
        try: send_account_email(user, 'reset')
        except Exception: app.logger.exception('Password reset email failed')
    return jsonify({'success':True, 'message':'اگر این ایمیل ثبت شده باشد، لینک بازیابی ارسال خواهد شد.'})

@app.route('/api/reset-password', methods=['POST'])
@limiter.limit("10 per hour")
def api_reset_password():
    data = request.get_json(silent=True) or {}
    user = token_user(str(data.get('token', '')), 'reset', 1800)
    if not user: return jsonify({'error':'لینک نامعتبر یا منقضی شده است'}), 400
    password = str(data.get('password', ''))
    if len(password) < 8 or len(password) > 128: return jsonify({'error':'رمز عبور باید بین ۸ و ۱۲۸ نویسه باشد'}), 400
    user.password_hash = hash_password(password)
    db.session.commit()
    return jsonify({'success':True})

@app.route('/verify-email/<token>')
def verify_email(token):
    user = token_user(token, 'verify', 86400)
    if not user:
        return render_template('email_result.html', success=False,
                               message='لینک تأیید نامعتبر یا منقضی شده است.'), 400
    user.email_verified = True
    db.session.commit()
    return render_template('email_result.html', success=True,
                           message='ایمیل شما با موفقیت تأیید شد!')


@app.route('/api/resend-verification', methods=['POST'])
@limiter.limit("5 per hour")
def resend_verification():
    data = request.get_json(silent=True) or {}
    email = str(data.get('email') or '').strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 150:
        return jsonify(error='آدرس ایمیل معتبر وارد کنید.'), 400
    message = 'اگر حسابی با این ایمیل در انتظار تأیید باشد، لینک جدید ارسال خواهد شد.'
    if not email_throttled(email, 'verify'):
        user = User.query.filter_by(email=email).first()
        if user and not user.email_verified:
            try:
                send_account_email(user, 'verify')
            except Exception:
                app.logger.exception('Resend verification failed')
    return jsonify(success=True, message=message)


@app.route("/api/me")
def api_me():
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True,
            "username":  current_user.username,
            "email":     current_user.email,
            "phone":     current_user.phone,
            "is_admin":  current_user.is_admin,
            "is_verified": bool(current_user.email_verified),
            "plan":      active_chat_plan(current_user),
            "chat_plan": active_chat_plan(current_user),
            "tutor_plan": active_tutor_plan(current_user),
            "chat_plan_expires_at":  current_user.chat_plan_expires_at.isoformat() if current_user.chat_plan_expires_at else None,
            "tutor_plan_expires_at": current_user.tutor_plan_expires_at.isoformat() if current_user.tutor_plan_expires_at else None,
        })
    return jsonify({"logged_in": False})

@app.route("/api/usage")
@login_required
def api_usage():
    from metering import tier_summary
    plan = active_chat_plan(current_user)
    tutor_tier_name = active_tutor_tier(current_user)
    return jsonify({'plan':plan,'tutor_plan':active_tutor_plan(current_user),
      'summary':tier_summary(db,current_user.id,'chat',PLAN_CONFIG[plan]),
      'tutor_summary':tier_summary(db,current_user.id,'tutor',TUTOR_PLAN_CONFIG[tutor_tier_name]),
      'actual_api_usage':api_meter_summary(db,current_user.id),
      'voice_plan':active_voice_tier(current_user),
      'voice_quotas':VOICE_PLANS[active_voice_tier(current_user)]})

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
@app.route("/api/transcribe", methods=["POST"])
@limiter.limit("15 per minute")
@login_required
def transcribe_audio():
    """Receive a browser-recorded audio file and return its transcription."""
    if not current_user.email_verified:
        return jsonify(error='Email verification required'),403
    if not os.getenv('OPENAI_API_KEY'):
        return jsonify(error='Voice service unavailable'),503

    audio = request.files.get("audio")
    if audio is None or not audio.filename:
        return jsonify({"error": "فایل صوتی دریافت نشد. دوباره ضبط کنید یا فایل صوتی را انتخاب نمایید."}), 400

    # Only audio formats supported by the transcription endpoint are accepted.
    allowed_extensions = {"webm", "mp4", "m4a", "mp3", "wav", "ogg", "mpeg", "mpga", "flac"}
    original_name = os.path.basename(audio.filename or "recording.webm")
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    mime = (audio.mimetype or "application/octet-stream").split(";", 1)[0].lower()
    mime_to_ext = {
        "audio/webm": "webm", "video/webm": "webm", "audio/mp4": "mp4", "video/mp4": "mp4",
        "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/mpeg": "mp3", "audio/mp3": "mp3",
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg", "application/ogg": "ogg",
        "audio/flac": "flac", "audio/x-flac": "flac", "audio/mpeg3": "mp3", "audio/mpga": "mpga"
    }
    if extension not in allowed_extensions:
        extension = mime_to_ext.get(mime, "")
    if extension not in allowed_extensions:
        return jsonify({"error": "قالب این فایل صوتی پشتیبانی نمی‌شود. فایل WebM، MP4، MP3 یا WAV را امتحان کنید."}), 415
    # Give OpenAI a useful content type even when a browser labels the blob generically.
    if mime not in mime_to_ext:
        mime = {
            "webm": "audio/webm", "mp4": "audio/mp4", "m4a": "audio/mp4", "mp3": "audio/mpeg",
            "mpeg": "audio/mpeg", "mpga": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
            "flac": "audio/flac"
        }.get(extension, "application/octet-stream")

    audio_bytes = audio.read()
    if not audio_bytes:
        return jsonify({"error": "ضبط خالی است. قبل از توقف ضبط، چند ثانیه صحبت کنید."}), 400
    if len(audio_bytes) > 20 * 1024 * 1024:
        return jsonify({"error": "فایل صوتی بیش از حد بزرگ است. یک ضبط کوتاه‌تر بفرستید."}), 413

    # This upload is for dictation and uses a small, fixed charge against voice
    # minutes. Separate transcription is not required for live Realtime itself.
    seconds = max(5, min(120, (len(audio_bytes)+23999)//24000))
    sid,quota=_reserve_voice_seconds(current_user.id,seconds,kind='dictation')
    if not sid:return jsonify(error='Voice allowance exhausted',quota=quota),429
    try:
        transcription = openai_client.audio.transcriptions.create(
            model=os.getenv("STT_MODEL", "gpt-transcribe"),
            file=(f"recording.{extension}", BytesIO(audio_bytes), mime),
            prompt=(
                "Transcribe the speech in its original language and script. The speaker may use Afghan Dari "
                "or Persian. Preserve the spoken language; do not translate it into English. Return only the words spoken."
            ),
        )
        transcript = (getattr(transcription, "text", "") or "").strip()
        if not transcript:
            return jsonify({"error": "گفتاری تشخیص داده نشد. نزدیک‌تر به میکروفون صحبت کنید و دوباره امتحان کنید."}), 422
        return jsonify({"text": transcript})
    except Exception:
        app.logger.exception("Audio transcription failed")
        return jsonify({"error": "تبدیل صدا به متن ناموفق شد. تنظیمات API و لاگ‌های Render را بررسی کنید."}), 502


def _owns_spoken_reply(user_id, spoken):
    # Prevent turning this protected TTS endpoint into a general text-audio service.
    def norm(s): return ' '.join(str(s or '').split())
    spoken=norm(spoken)
    rows=db.session.query(Message.content).join(Conversation).filter(
       Conversation.user_id==user_id,Message.role=='assistant').order_by(Message.id.desc()).limit(50).all()
    if any(norm(s)==spoken for (s,) in rows):return True
    _ensure_lesson_storage()
    raw=db.session.execute(sql_text('''SELECT history FROM tutor_lesson_histories
       WHERE user_id=:uid ORDER BY updated_at DESC LIMIT 30'''),{'uid':user_id}).all()
    for (value,) in raw:
        try:
            if any(m.get('role')=='assistant' and norm(m.get('content'))==spoken
                   for m in json.loads(value or '[]')):return True
        except (ValueError,TypeError):pass
    # Legacy Tutor messages existed before per-topic storage.
    for progress in TutorProgress.query.filter_by(user_id=user_id).all():
        try:
            if any(m.get('role')=='assistant' and norm(m.get('content'))==spoken
                   for m in json.loads(progress.chat_history or '[]')):return True
        except (TypeError,ValueError):pass
    return False


@app.route('/api/tts', methods=['POST'])
@limiter.limit("20 per minute")
@login_required
def text_to_speech():
    if not current_user.email_verified:
        return jsonify(error='Email verification required'),403
    if not os.getenv('OPENAI_API_KEY'):
        return jsonify(error='Voice service unavailable'),503
    payload=request.get_json(silent=True) or {}
    content=payload.get('text')
    if not isinstance(content,str) or not content.strip() or len(content)>2000:
        return jsonify(error='Invalid reply text (max 2000 characters)'),400
    content=' '.join(content.split())
    if not _owns_spoken_reply(current_user.id,content):
        return jsonify(error='You can only play your own saved replies'),403
    # Conservative speech-time charge, shared with live voice. Audio is not generated
    # before the quota reservation commits, preventing concurrent overuse.
    seconds=max(8,min(250,(len(content)+7)//8+5))
    sid,quota=_reserve_voice_seconds(current_user.id,seconds,kind='listen')
    if not sid: return jsonify(error='Voice allowance exhausted',quota=quota),429
    try:
        speech=openai_client.audio.speech.create(
            model=os.getenv('TTS_MODEL','gpt-4o-mini-tts'),
            voice=os.getenv('KHAYYAM_VOICE','cedar'),input=content,
            instructions=('Read this text faithfully in warm, natural Afghan Dari, with a calm '
                          'teacher style and moderate pace; no extra words.'),response_format='mp3')
        audio=speech.read()
        if not audio: raise RuntimeError('Empty speech response')
        return send_file(BytesIO(audio),mimetype='audio/mpeg',as_attachment=False,
                         download_name='khayyam-reply.mp3',max_age=0)
    except Exception:
        # Unknown provider billing state; retain quota charge to avoid abuse.
        app.logger.exception('Text playback failed')
        return jsonify(error='Voice generation failed; retry later'),502

@app.route("/api/chat", methods=["POST"])
@limiter.limit("30 per minute;400 per hour")
def chat():
    has_doc = False
    if request.content_type and 'multipart/form-data' in request.content_type:
        user_message = request.form.get("message", "")
        try:
            history = json.loads(request.form.get("history", "[]"))
        except ValueError:
            return jsonify(error='Invalid conversation history'), 400
        conv_id      = request.form.get("conversation_id")
        image_b64    = None
        image_type   = None

        if 'image' in request.files:
            img          = request.files['image']
            img_bytes    = img.read()
            if len(img_bytes) > 4 * 1024 * 1024:
                return jsonify(error='Image too large (4MB limit)'),413
            image_type = img.content_type or 'image/jpeg'
            if image_type not in ('image/jpeg','image/png','image/webp'):
                return jsonify(error='Unsupported image format'),415
            image_b64    = base64.b64encode(img_bytes).decode('utf-8')
            user_message = user_message or "این تصویر را به دری توضیح بده"

        if 'document' in request.files:
            doc          = request.files['document']
            doc_bytes    = doc.read()
            doc_text     = extract_text_from_file(doc_bytes, doc.filename)
            if len(doc_text) > 12000:
                doc_text = doc_text[:12000] + "\n[... ادامه فایل به دلیل طولانی بودن کوتاه شد]"
            has_doc      = True
            user_message = (user_message or "این سند را خلاصه کن") + \
                           f"\n\n[محتوای فایل]:\n{doc_text}"
    else:
        data         = request.get_json(silent=True) or {}
        user_message = data.get("message", "")
        history      = data.get("history", [])
        conv_id      = data.get("conversation_id")
        image_b64    = None
        image_type   = None

    if not isinstance(history,list):
        return jsonify(error='Invalid conversation history'),400
    history=[{'role':str(m.get('role')),'content':str(m.get('content',''))[:6000]}
             for m in history[-10:] if isinstance(m,dict) and m.get('role') in ('user','assistant')]
    if not isinstance(user_message,str) or len(user_message)>15000:
        return jsonify(error='Message too long'),400
    user_id = current_user.id if current_user.is_authenticated else None
    plan    = active_chat_plan(current_user if current_user.is_authenticated else None)

    # A follow-up must NEVER silently become a new conversation when its ID is
    # missing or stale. For logged-in students, the database is the authoritative
    # source of chat context; browser-side history can be outdated after reload.
    conv = None
    if conv_id not in (None, '', 0, '0'):
        try:
            conv_id = int(conv_id)
        except (TypeError, ValueError):
            return jsonify(error='Invalid conversation ID'), 400
        if user_id is None:
            return jsonify(error='Log in to continue a saved conversation'), 401
        conv = Conversation.query.filter_by(id=conv_id, user_id=user_id).first()
        if conv is None:
            return jsonify(error='Conversation not found; reopen it from your history'), 404
        prior = (Message.query.filter_by(conversation_id=conv.id)
                 .order_by(Message.id.desc()).limit(10).all())
        history = [{'role':m.role,'content':m.content[:6000]}
                   for m in reversed(prior) if m.role in ('user','assistant')]
    elif user_id is not None and history:
        # Recovery path: the first response may have reached the browser without
        # its conversation ID being applied. Do not reject the student's next turn.
        # Prefer the most recently saved conversation only when its last messages
        # exactly match the supplied client history. Otherwise start a new chat.
        recent = (Conversation.query.filter_by(user_id=user_id)
                  .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                  .limit(5).all())
        for candidate in recent:
            saved = (Message.query.filter_by(conversation_id=candidate.id)
                     .order_by(Message.id.desc()).limit(min(len(history), 10)).all())
            saved = list(reversed(saved))
            if saved and len(saved) == min(len(history), 10) and all(
                m.role == h['role'] and m.content == h['content']
                for m, h in zip(saved, history[-len(saved):])
            ):
                conv = candidate
                break
        # If no exact match, retain supplied history and create a new conversation
        # after the provider succeeds. This is safer than blocking all follow-ups.

    # Decide HOW to answer before spending anything (see router.py).
    intent = khr.classify(user_message, history, has_image=bool(image_b64), has_doc=has_doc)
    switched = False
    reset_ts = None
    if intent['level'] == 'canned':
        reply = khr.canned_reply(user_message) or ''
        model_used = 'canned'            # free: greetings never reach a paid model
    else:
        user_memories = get_user_memories(user_id) if user_id else None
        try:
            reply, switched, reset_ts, model_used = smart_chat(
                system_prompt=build_system_prompt(user_memories, user_message,
                                                  intent['kind'], intent['level']),
                history=khr.trim_history(history),
                user_message=user_message,
                user_id=user_id,
                plan=plan,
                temperature=0.7,
                image_b64=image_b64,
                image_type=image_type,
                intent=intent
            )
        except ValueError:
            return jsonify(error='پیام یا فایل خیلی طولانی است؛ آن را کوتاه‌تر کنید.'), 413
        except Exception:
            app.logger.exception('Chat completion failed')
            return jsonify(error='سرویس گفتگو موقتاً در دسترس نیست. دوباره تلاش کنید.'), 503

    if model_used == 'limited':
        return jsonify({'reply': reply, 'limited': True, 'reset_ts': reset_ts,
                        'switch_notice': False}), 429
    if model_used == 'error':
        return jsonify(error=reply), 503
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
@login_required
@limiter.limit("20 per minute")
def persona_chat():
    data           = request.get_json(silent=True) or {}
    user_message   = data.get("message", "")
    history        = data.get("history", [])
    persona_prompt = data.get("persona_prompt", "")
    if not isinstance(persona_prompt,str) or len(persona_prompt)>2000 or not isinstance(history,list):
        return jsonify(error='Invalid input'),400
    history=[{'role':str(m.get('role')),'content':str(m.get('content',''))[:6000]}
             for m in history[-10:] if isinstance(m,dict) and m.get('role') in ('user','assistant')]
    if not isinstance(user_message,str) or len(user_message)>15000:
        return jsonify(error='Message too long'),400

    user_id = current_user.id if current_user.is_authenticated else None
    plan    = active_chat_plan(current_user if current_user.is_authenticated else None)

    try:
        reply, _, _, _ = smart_chat(
            system_prompt=persona_prompt,
            history=history[-10:],
            user_message=user_message,
            user_id=user_id,
            plan=plan,
            temperature=0.9
        )
    except Exception as e:
        print(f"Persona chat error: {e}")
        reply = "متأسفم، مشکلی پیش آمد."

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
    data    = request.get_json(silent=True) or {}
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
@app.route("/api/admin/users", methods=["GET"])
@login_required
@admin_required
def get_users():
    users = User.query.order_by(User.created_at.desc()).limit(1000).all()
    return jsonify([{
        "id":         u.id,
        "username":   u.username,
        "email":      u.email,
        "phone":      u.phone,
        "is_admin":   u.is_admin,
        "is_verified": bool(u.email_verified),
        "plan":       active_chat_plan(u),
        "tutor_plan": active_tutor_plan(u),
        "plan_expires_at":       u.chat_plan_expires_at.isoformat() if u.chat_plan_expires_at else None,
        "tutor_plan_expires_at": u.tutor_plan_expires_at.isoformat() if u.tutor_plan_expires_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None
    } for u in users])

@app.route("/api/admin/users/<int:user_id>/plan", methods=["POST"])
@login_required
@admin_required
def update_user_plan(user_id):
    """Body: {plan, kind?('chat'|'tutor'), months? (renew/extend), expires_at? (explicit date),
    notify?}. plan='free' revokes."""
    data=request.get_json(silent=True) or {}
    plan=str(data.get('plan') or 'free')
    kind=str(data.get('kind') or ('tutor' if plan=='tutor_pro' else 'chat'))
    user=db.session.get(User,user_id)
    if not user: return jsonify(success=False,error='not found'),404
    expires=None
    if data.get('expires_at'):
        try: expires=datetime.fromisoformat(str(data['expires_at']))
        except (TypeError,ValueError):return jsonify(success=False,error='Invalid expiration'),400
    try:
        grant_plan(user,kind,plan,months=data.get('months') or 1,actor=current_user.username,
                   expires_at=expires,notify=data.get('notify',True) is not False)
    except ValueError as exc:
        return jsonify(success=False,error=str(exc)),400
    return jsonify(success=True,chat_plan=active_chat_plan(user),tutor_plan=active_tutor_plan(user),
                   chat_plan_expires_at=user.chat_plan_expires_at.isoformat() if user.chat_plan_expires_at else None,
                   tutor_plan_expires_at=user.tutor_plan_expires_at.isoformat() if user.tutor_plan_expires_at else None)

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
    data      = request.get_json(silent=True) or {}
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
    data      = request.get_json(silent=True) or {}
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
    data     = request.get_json(silent=True) or {}
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

====================
وضعیت درس
====================
{"این شروع درس است — با یک معرفی جذاب شروع کن" if chat_history_len == 0 else "درس در حال جریان است — ادامه بده"}

فقط درس بده. اگر کاربر از موضوع خارج شد، آرام او را به موضوع برگردان."""

# Step 3: durable per-topic history shared by text and realtime voice.
# The old TutorProgress.chat_history remains untouched for compatibility.
@_once
def _ensure_lesson_storage():
    with db.engine.begin() as conn:
        conn.execute(sql_text("""CREATE TABLE IF NOT EXISTS tutor_lesson_histories (
            user_id INTEGER NOT NULL,
            subject VARCHAR(60) NOT NULL,
            topic_key VARCHAR(100) NOT NULL,
            history TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, subject, topic_key)
        )"""))


def _lesson_history(user_id, subject, topic_key, lock=False):
    _ensure_lesson_storage()
    suffix = ' FOR UPDATE' if lock and db.engine.dialect.name == 'postgresql' else ''
    row = db.session.execute(sql_text("""SELECT history FROM tutor_lesson_histories
        WHERE user_id=:uid AND subject=:subject AND topic_key=:topic""" + suffix),
        {'uid': user_id, 'subject': subject, 'topic': topic_key}).first()
    if not row:
        return []
    try:
        result = json.loads(row[0] or '[]')
        return result if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []


def _save_lesson_history(user_id, subject, topic_key, history):
    _ensure_lesson_storage()
    data = json.dumps(history[-80:], ensure_ascii=False)
    # Postgres + SQLite compatible upsert; use DB atomic write.
    db.session.execute(sql_text("""INSERT INTO tutor_lesson_histories
        (user_id, subject, topic_key, history, updated_at)
        VALUES (:uid, :subject, :topic, :history, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id, subject, topic_key) DO UPDATE SET
        history=EXCLUDED.history, updated_at=CURRENT_TIMESTAMP"""),
        {'uid': user_id, 'subject': subject, 'topic': topic_key, 'history': data})


def _topic_info(subject, level, topic_key):
    if subject not in CURRICULUM:
        return None
    return next((t for t in CURRICULUM[subject]['levels'].get(level, {}).get('topics', [])
                 if t['key'] == topic_key), None)


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
    """Returns student's progress for a specific subject."""
    progress = get_or_create_progress(current_user.id, subject)
    user = User.query.get(current_user.id)
    badges = StudentBadge.query.filter_by(user_id=current_user.id).all()

    return jsonify({
        'progress':   progress.to_dict(),
        'total_xp':   user.total_xp or 0,
        'badges':     [b.to_dict() for b in badges],
        'chat_history': json.loads(progress.chat_history or '[]')
    })

@app.route('/api/tutor/lesson-history/<subject>/<topic_key>')
@login_required
def get_tutor_lesson_history(subject, topic_key):
    if subject not in CURRICULUM or not any(
        t['key'] == topic_key for level in CURRICULUM[subject]['levels'].values()
        for t in level['topics']):
        return jsonify(error='Lesson not found'), 404
    return jsonify(history=_lesson_history(current_user.id, subject, topic_key))


@app.route("/api/tutor/start-topic", methods=["POST"])
@login_required
def start_topic():
    """Student starts or resumes a topic."""
    data       = request.get_json(silent=True) or {}
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
        "history": _lesson_history(current_user.id, subject, topic_key)
    })
@app.route("/api/tutor/chat", methods=["POST"])
@limiter.limit("30 per minute")
@login_required
def tutor_chat_new():
    data      = request.get_json(silent=True) or {}
    subject   = data.get("subject")
    topic_key = data.get("topic_key")
    try: level = int(data.get("level", 1))
    except (TypeError, ValueError): return jsonify({"error": "سطح نامعتبر"}), 400
    message   = data.get("message", "")
    if not isinstance(message, str) or not message.strip() or len(message) > 6000:
        return jsonify({"error": "پیام نامعتبر یا خیلی طولانی است"}), 400

    if subject not in CURRICULUM:
        return jsonify({"error": "مضمون پیدا نشد"}), 400

    topic_info = None
    for t in CURRICULUM[subject]['levels'].get(level, {}).get('topics', []):
        if t['key'] == topic_key:
            topic_info = t
            break

    if not topic_info:
        return jsonify({"error": "موضوع پیدا نشد"}), 400

    progress     = get_or_create_progress(current_user.id, subject)
    chat_history = _lesson_history(current_user.id, subject, topic_key)
    system_prompt = build_tutor_system_prompt(
        subject, topic_info['title'], topic_info['desc'], len(chat_history)
    )
    completed = json.loads(progress.completed_topics or '[]')
    if topic_key in completed:
        system_prompt += '\nاین موضوع قبلاً تکمیل شده؛ در صورت نیاز مرور و تمرین ارائه کن.'
    elif chat_history:
        system_prompt += '\nاین درس قبلاً شروع شده؛ از گفتگوهای قبلی همین موضوع ادامه بده.'
    plan = active_tutor_tier(current_user)

    try:
        reply, is_limited, reset_ts = smart_tutor_chat(
            system_prompt=system_prompt,
            history=chat_history,
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

    # save history
    chat_history.append({"role": "user", "content": message})
    chat_history.append({"role": "assistant", "content": reply})
    if len(chat_history) > 60:
        chat_history = chat_history[-60:]

    _save_lesson_history(current_user.id, subject, topic_key, chat_history)
    progress.chat_history     = json.dumps(chat_history, ensure_ascii=False)
    progress.last_activity    = datetime.utcnow()
    progress.last_topic_title = topic_info['title']
    db.session.commit()

    return jsonify({"reply": reply})
@app.route("/api/tutor/complete-topic", methods=["POST"])
@login_required
def complete_topic():
    """Student marks a topic as complete."""
    data      = request.get_json(silent=True) or {}
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

# Step 4: server-scored quizzes; never trust browser-submitted scores or answer keys.
@_once
def _quiz_claim_table():
    with db.engine.begin() as conn:
        conn.execute(sql_text("""CREATE TABLE IF NOT EXISTS tutor_quiz_claims (
            attempt_id VARCHAR(36) PRIMARY KEY, user_id INTEGER NOT NULL,
            subject VARCHAR(40) NOT NULL, level INTEGER NOT NULL,
            score INTEGER NOT NULL, xp_earned INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"""))

@app.route("/api/tutor/generate-quiz", methods=["POST"])
@limiter.limit("10 per minute")
@login_required
def generate_quiz():
    data = request.get_json(silent=True) or {}
    subject = str(data.get('subject') or '')
    try: level = int(data.get('level', 1))
    except (ValueError, TypeError): return jsonify(error='Invalid level'), 400
    level_data = CURRICULUM.get(subject, {}).get('levels', {}).get(level)
    if not level_data: return jsonify(error='Invalid lesson'), 400
    practice = data.get('practice') is True
    topic_key = str(data.get('topic_key') or '')
    topic = next((t for t in level_data['topics'] if t['key'] == topic_key), None)
    if practice and not topic: return jsonify(error='Invalid practice topic'), 400
    count = 1 if practice else 8
    titles = topic['title'] if practice else ', '.join(t['title'] for t in level_data['topics'])
    prompt = f"""برای مضمون {CURRICULUM[subject]['name']} سطح {level_data['title']}، {count} سوال چهارگزینه‌ای مستقل به دری افغانستان بساز. موضوع: {titles}.
سوال‌ها دقیق، بدون ابهام، و دارای چهار گزینه باشند. فقط JSON خالص به شکل
{{"questions":[{{"q":"سوال","options":["الف","ب","ج","د"],"correct":0,"explanation":"دلیل"}}]}}
مقدار correct عدد صحیح 0 تا 3 باشد. دقیقا {count} سوال بساز."""
    try:
        answer,_,_,answer_model=_metered_completion('tutor','Generate valid educational quiz JSON.',
            [{'role':'user','content':prompt}],current_user.id,
            active_tutor_tier(current_user),max_output=2400,level='standard',
            json_mode=True,intent_label='quiz')
        if not answer:return jsonify(error='Tutor allowance exhausted' if answer_model=='limited' else 'سرویس موقتاً در دسترس نیست'),(429 if answer_model=='limited' else 502)
        raw=answer.replace('```json','').replace('```','').strip()
        questions = json.loads(raw)['questions']
        if len(questions) != count: raise ValueError('Wrong question count')
        clean = []
        for q in questions:
            if not isinstance(q['q'], str) or len(q['options']) != 4 or not all(isinstance(x,str) for x in q['options']):
                raise ValueError('Invalid question')
            correct = int(q['correct'])
            if correct not in range(4): raise ValueError('Invalid answer')
            clean.append({'q':q['q'][:700], 'options':[x[:300] for x in q['options']],
                          'correct':correct, 'explanation':str(q.get('explanation',''))[:700]})
        # Signed answer key, short lived; browser receives questions without answers.
        attempt_id = str(uuid.uuid4())
        token = URLSafeTimedSerializer(app.secret_key).dumps(
            {'uid':current_user.id,'subject':subject,'level':level,
             'practice':practice,'topic_key':topic_key,'attempt_id':attempt_id,
             'questions':clean}, salt='khayyam-quiz-v4')
        public = [{k:q[k] for k in ('q','options')} for q in clean]
        return jsonify(questions=public, quiz_token=token, practice=practice)
    except Exception:
        app.logger.exception('Quiz generation failed')
        return jsonify(error='خطا در ساخت کوییز؛ دوباره تلاش کنید'), 502

@app.route('/api/tutor/submit-quiz', methods=['POST'])
@limiter.limit("20 per minute")
@login_required
def submit_quiz():
    data = request.get_json(silent=True) or {}
    try:
        payload = URLSafeTimedSerializer(app.secret_key).loads(
            str(data.get('quiz_token') or ''), salt='khayyam-quiz-v4', max_age=7200)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return jsonify(error='Quiz expired or invalid'), 403
    if payload.get('uid') != current_user.id or payload.get('practice'):
        return jsonify(error='Invalid graded quiz'), 403
    answers = data.get('answers')
    questions = payload['questions']
    if not isinstance(answers, list) or len(answers) != len(questions) or any(type(a) is not int or a not in range(4) for a in answers):
        return jsonify(error='Invalid answers'), 400
    subject, level = payload['subject'], payload['level']
    if level not in CURRICULUM.get(subject, {}).get('levels', {}):
        return jsonify(error='Invalid subject'), 400
    score = round(100 * sum(a == q['correct'] for a,q in zip(answers,questions)) / len(questions))
    passed = score >= 70
    quiz_key = f'{subject}_level{level}'
    attempt_id = payload['attempt_id']
    try:
        _quiz_claim_table()
        # Serialize awards for the user across simultaneous submissions (PostgreSQL).
        lock = ' FOR UPDATE' if db.engine.dialect.name == 'postgresql' else ''
        db.session.execute(sql_text('SELECT id FROM users WHERE id=:uid' + lock), {'uid':current_user.id}).first()
        existing = db.session.execute(sql_text('SELECT score,xp_earned FROM tutor_quiz_claims WHERE attempt_id=:aid'), {'aid':attempt_id}).mappings().first()
        if existing:
            db.session.rollback()
            return jsonify(passed=existing['score'] >= 70, score=existing['score'], xp_earned=existing['xp_earned'],
                           total_xp=db.session.get(User,current_user.id).total_xp or 0, already_submitted=True)
        progress = TutorProgress.query.filter_by(user_id=current_user.id, subject=subject).first()
        if not progress:
            progress = TutorProgress(user_id=current_user.id, subject=subject)
            db.session.add(progress)
            db.session.flush()
        completed = json.loads(progress.completed_quizzes or '[]')
        xp = 0
        if passed and quiz_key not in completed:
            xp = XP_REWARDS['quiz_perfect'] if score == 100 else XP_REWARDS['quiz_pass']
            completed.append(quiz_key)
            progress.completed_quizzes = json.dumps(completed)
            user = db.session.get(User,current_user.id)
            user.total_xp = (user.total_xp or 0) + xp
            progress.subject_xp = (progress.subject_xp or 0) + xp
            if score == 100 and not StudentBadge.query.filter_by(user_id=current_user.id, badge_key='quiz_perfect', subject=subject).first():
                db.session.add(StudentBadge(user_id=current_user.id, badge_key='quiz_perfect',
                                            badge_title=BADGES['quiz_perfect']['title'],
                                            badge_emoji=BADGES['quiz_perfect']['emoji'],subject=subject))
        db.session.add(QuizResult(user_id=current_user.id,subject=subject,level=level,quiz_key=quiz_key,
                                 score=score,passed=passed,xp_earned=xp))
        db.session.execute(sql_text('INSERT INTO tutor_quiz_claims (attempt_id,user_id,subject,level,score,xp_earned) VALUES (:aid,:uid,:sub,:lvl,:score,:xp)'),
                           {'aid':attempt_id,'uid':current_user.id,'sub':subject,'lvl':level,'score':score,'xp':xp})
        db.session.commit()
        return jsonify(passed=passed,score=score,xp_earned=xp,total_xp=db.session.get(User,current_user.id).total_xp or 0,
                       review=[{'correct':q['correct'],'explanation':q['explanation']} for q in questions])
    except Exception:
        db.session.rollback()
        app.logger.exception('Quiz submission failed')
        return jsonify(error='Could not save quiz result'), 500

@app.route('/api/tutor/check-practice', methods=['POST'])
@login_required
def check_practice():
    data = request.get_json(silent=True) or {}
    try:
        payload = URLSafeTimedSerializer(app.secret_key).loads(str(data.get('quiz_token') or ''),salt='khayyam-quiz-v4',max_age=7200)
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return jsonify(error='Practice expired'), 403
    if payload.get('uid') != current_user.id or not payload.get('practice'):
        return jsonify(error='Invalid practice'), 403
    answer = data.get('answer')
    if type(answer) is not int or answer not in range(4): return jsonify(error='Invalid answer'),400
    q = payload['questions'][0]
    return jsonify(correct=answer == q['correct'], correct_index=q['correct'], explanation=q['explanation'],xp_earned=0)

@app.route("/api/tutor/placement-test", methods=["POST"])
@limiter.limit("5 per minute")
@login_required
def placement_test():
    """Generates a placement test for a subject."""
    data    = request.get_json(silent=True) or {}
    subject = data.get("subject")
    if subject not in CURRICULUM:
        return jsonify({"error": "مضمون پیدا نشد"}), 400

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

    try:
        answer,_,_,answer_model=_metered_completion('tutor','Generate a placement-test JSON object.',
            [{'role':'user','content':prompt}],current_user.id,
            active_tutor_tier(current_user),max_output=2000,level='standard',
            json_mode=True,intent_label='placement')
        if not answer:return jsonify(error='Tutor allowance exhausted' if answer_model=='limited' else 'سرویس موقتاً در دسترس نیست'),(429 if answer_model=='limited' else 502)
        raw = answer.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        return jsonify(json.loads(raw))
    except Exception as e:
        print(f"Placement test error: {e}")
        return jsonify({"error": "خطا در ساخت تست"}), 500

@app.route("/api/tutor/placement-result", methods=["POST"])
@login_required
def placement_result():
    """Calculates recommended level from placement test answers."""
    data    = request.get_json(silent=True) or {}
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


# ---------------------------------------------------------------------------
# Khayyam Live Voice v2 — Chat and Tutor share one realtime engine,
# but have different prompts and save transcripts to different histories.
# ---------------------------------------------------------------------------
from itsdangerous import URLSafeSerializer


def _voice_context(data):
    mode = data.get("mode", "chat")
    if mode not in ("chat", "tutor"):
        raise ValueError("Invalid voice mode")
    if mode == "chat":
        conv_id = data.get("conversation_id")
        conv = None
        if conv_id not in (None, "", 0):
            try:
                conv_id = int(conv_id)
            except (TypeError, ValueError):
                raise ValueError("Invalid conversation")
            conv = Conversation.query.filter_by(id=conv_id, user_id=current_user.id).first()
            if not conv:
                raise ValueError("Conversation not found")
        history = []
        if conv:
            history = Message.query.filter_by(conversation_id=conv.id).order_by(Message.id.desc()).limit(12).all()
            history.reverse()
        prompt = (
            "You are Khayyam AI in GENERAL CONVERSATION mode, not a tutor. "
            "Be a natural, warm, intelligent conversation partner. Follow the user's interests, "
            "ask questions only when helpful, and do not turn ordinary conversations into lessons. "
            "Respond in the user's language; when using Dari, prefer Afghan Dari vocabulary. "
            "Keep speech conversational and fairly concise. "
        )
        if history:
            context = "\n".join(f"{m.role}: {m.content[:800]}" for m in history)
            prompt += "\nRecent chat context (do not read aloud):\n" + context
        return mode, {"conversation_id": conv.id if conv else None}, prompt

    subject = str(data.get("subject") or "")
    topic_key = str(data.get("topic_key") or "")
    try:
        level = int(data.get("level", 1))
    except (TypeError, ValueError):
        raise ValueError("Invalid level")
    if subject not in CURRICULUM:
        raise ValueError("Unknown subject")
    topics = CURRICULUM[subject]["levels"].get(level, {}).get("topics", [])
    topic = next((t for t in topics if t["key"] == topic_key), None)
    if not topic:
        raise ValueError("Unknown lesson")
    progress = get_or_create_progress(current_user.id, subject)
    history = _lesson_history(current_user.id, subject, topic_key)[-12:]
    prompt = build_tutor_system_prompt(subject, topic["title"], topic["desc"], len(history))
    prompt += ("\nYou are now teaching in a LIVE VOICE lesson. Be a patient, engaging teacher. "
               "Explain one concept at a time, check comprehension, and stay on this subject. "
               "Speak in the student's language; prefer Afghan Dari vocabulary when speaking Dari. ")
    if history:
        prompt += "\nRecent lesson context (do not read aloud):\n" + "\n".join(
            f"{m.get('role')}: {str(m.get('content', ''))[:700]}" for m in history)
    return mode, {"subject": subject, "topic_key": topic_key, "level": level}, prompt


@app.route("/voice")
@login_required
def live_voice_page():
    if not current_user.email_verified:
        return redirect(url_for("check_email_page"))
    return render_template("live_voice.html")


# Durable voice-session mapping and turn-level deduplication. Additive schema only.
# Created lazily so existing user data and conversation tables are untouched.
@_once
def _ensure_voice_storage():
    with db.engine.begin() as connection:
        connection.execute(sql_text("""CREATE TABLE IF NOT EXISTS voice_save_sessions (
            session_id VARCHAR(36) PRIMARY KEY,
            user_id INTEGER NOT NULL,
            mode VARCHAR(12) NOT NULL,
            conversation_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""))
        connection.execute(sql_text("""CREATE TABLE IF NOT EXISTS voice_saved_turns (
            session_id VARCHAR(36) NOT NULL,
            turn_id VARCHAR(160) NOT NULL,
            PRIMARY KEY (session_id, turn_id)
        )"""))


# UTC day and calendar-month quotas shared by Chat, Tutor, Listen, Dictation.
VOICE_WARNING_SECONDS = 120
VOICE_MODEL=os.getenv('REALTIME_MODEL','gpt-realtime-2.1-mini')

def _voice_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _voice_day():
    return _voice_now().date().isoformat()

@_once
def _ensure_voice_quota_storage():
    with db.engine.begin() as conn:
        conn.execute(sql_text('''CREATE TABLE IF NOT EXISTS voice_daily_usage (
            user_id INTEGER NOT NULL, usage_day VARCHAR(10) NOT NULL,
            used_seconds INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, usage_day))'''))
        conn.execute(sql_text('''CREATE TABLE IF NOT EXISTS voice_quota_sessions (
            session_id VARCHAR(36) PRIMARY KEY, user_id INTEGER NOT NULL,
            usage_day VARCHAR(10) NOT NULL, started_at TIMESTAMP NOT NULL,
            reserved_seconds INTEGER NOT NULL, ended_at TIMESTAMP,
            charged_seconds INTEGER, kind VARCHAR(12) NOT NULL DEFAULT 'live'
        )'''))
        from sqlalchemy import inspect
        columns={c['name'] for c in inspect(conn).get_columns('voice_quota_sessions')}
        if 'kind' not in columns:
            conn.execute(sql_text("ALTER TABLE voice_quota_sessions ADD COLUMN kind VARCHAR(12) NOT NULL DEFAULT 'live'"))
        conn.execute(sql_text('''CREATE TABLE IF NOT EXISTS voice_monthly_locks (
            user_id INTEGER NOT NULL, month_key VARCHAR(7) NOT NULL,
            PRIMARY KEY(user_id,month_key))'''))

def _voice_quota_lock(uid,day):
    month=day[:7]
    db.session.execute(sql_text('''INSERT INTO voice_monthly_locks(user_id,month_key)
        VALUES (:uid,:m) ON CONFLICT(user_id,month_key) DO NOTHING'''),{'uid':uid,'m':month})
    db.session.execute(sql_text('''INSERT INTO voice_daily_usage(user_id,usage_day,used_seconds)
        VALUES (:uid,:day,0) ON CONFLICT(user_id,usage_day) DO NOTHING'''),{'uid':uid,'day':day})
    lock=' FOR UPDATE' if db.engine.dialect.name=='postgresql' else ''
    db.session.execute(sql_text('SELECT month_key FROM voice_monthly_locks WHERE user_id=:uid AND month_key=:m'+lock),{'uid':uid,'m':month}).one()
    return db.session.execute(sql_text('SELECT used_seconds FROM voice_daily_usage WHERE user_id=:uid AND usage_day=:day'+lock),{'uid':uid,'day':day}).scalar_one()

def _voice_quota_reconcile(uid,day,now):
    # Mark abandoned sessions consumed when their reservation duration elapsed.
    # The provider session may still be alive: see beta-mode risk in DEPLOYMENT_NOTES.
    rows=db.session.execute(sql_text('''SELECT session_id,started_at,reserved_seconds FROM
        voice_quota_sessions WHERE user_id=:uid AND ended_at IS NULL'''),
        {'uid':uid}).mappings().all()
    for row in rows:
        elapsed=max(0,int((now - (datetime.fromisoformat(row['started_at']) if isinstance(row['started_at'],str) else row['started_at'])).total_seconds()))
        if elapsed>=row['reserved_seconds']:
            db.session.execute(sql_text('''UPDATE voice_quota_sessions SET ended_at=:now,
              charged_seconds=:charged WHERE session_id=:sid AND ended_at IS NULL'''),
              {'now':now,'charged':row['reserved_seconds'],'sid':row['session_id']})
    result=db.session.execute(sql_text('''SELECT
       COALESCE(SUM(CASE WHEN usage_day=:day THEN COALESCE(charged_seconds,reserved_seconds) ELSE 0 END),0) AS used_day,
       COALESCE(SUM(COALESCE(charged_seconds,reserved_seconds)),0) AS used_month
       FROM voice_quota_sessions WHERE user_id=:uid AND usage_day LIKE :month'''),
       {'uid':uid,'day':day,'month':day[:7]+'%'}).mappings().first()
    return int(result['used_day']),int(result['used_month'])

def _voice_snapshot_locked(uid,now):
    day=now.date().isoformat()
    _voice_quota_lock(uid,day)
    used_day,used_month=_voice_quota_reconcile(uid,day,now)
    profile=VOICE_PLANS[active_voice_tier(db.session.get(User,uid))]
    daily=max(0,profile['daily']-used_day)
    monthly=max(0,profile['monthly']-used_month)
    return {'plan':active_voice_tier(db.session.get(User,uid)),
      'limit_seconds':profile['daily'],'monthly_limit_seconds':profile['monthly'],
      'used_seconds':used_day,'monthly_used_seconds':used_month,
      'daily_remaining_seconds':daily,'monthly_remaining_seconds':monthly,
      'remaining_seconds':min(daily,monthly),
      'warning_seconds':VOICE_WARNING_SECONDS,'reset_timezone':'UTC'}

def _voice_quota_snapshot(uid):
    _ensure_voice_quota_storage()
    try:
        result=_voice_snapshot_locked(uid,_voice_now())
        db.session.commit(); return result
    except Exception:
        db.session.rollback();raise

def _reserve_voice_seconds(uid,seconds,kind='live'):
    _ensure_voice_quota_storage()
    try:
        now=_voice_now();quota=_voice_snapshot_locked(uid,now)
        seconds=int(seconds)
        if seconds<1 or seconds>3600 or quota['remaining_seconds']<seconds:
            db.session.rollback(); return None,quota
        if kind=='live':
            exists=db.session.execute(sql_text('''SELECT 1 FROM voice_quota_sessions
                WHERE user_id=:uid AND ended_at IS NULL LIMIT 1'''),{'uid':uid}).first()
            if exists:
                db.session.rollback();return None,quota
        sid=str(uuid.uuid4())
        db.session.execute(sql_text('''INSERT INTO voice_quota_sessions
             (session_id,user_id,usage_day,started_at,reserved_seconds,kind,ended_at,charged_seconds)
             VALUES(:sid,:uid,:day,:now,:seconds,:kind,:ended,:charged)'''),
             {'sid':sid,'uid':uid,'day':now.date().isoformat(),'now':now,
              'seconds':seconds,'kind':kind,
              'ended':now if kind!='live' else None,
              'charged':seconds if kind!='live' else None})
        db.session.commit(); return sid,quota
    except Exception:
        db.session.rollback();raise

@app.route("/api/voice/quota", methods=["GET"])
@login_required
def voice_quota_status():
    if not current_user.email_verified:
        return jsonify(error="Email verification required"), 403
    try:
        return jsonify(_voice_quota_snapshot(current_user.id))
    except Exception:
        app.logger.exception("Voice quota status failed")
        return jsonify(error="Voice allowance temporarily unavailable"), 503


@app.route("/api/voice/finish", methods=["POST"])
@login_required
def voice_quota_finish():
    data = request.get_json(silent=True) or {}
    try:
        receipt = URLSafeSerializer(app.secret_key, salt="khayyam-voice-v2").loads(
            str(data.get("receipt") or ""))
        if receipt.get("uid") != current_user.id:
            return jsonify(error="Invalid voice session"), 403
        sid = str(receipt.get("sid") or "")
        _ensure_voice_quota_storage()
        now = _voice_now()
        row = db.session.execute(sql_text("""SELECT user_id, started_at,
            reserved_seconds, ended_at, usage_day FROM voice_quota_sessions
            WHERE session_id=:sid""" +
            (" FOR UPDATE" if db.engine.dialect.name == "postgresql" else "")),
            {"sid":sid}).mappings().first()
        if not row or row["user_id"] != current_user.id:
            db.session.rollback()
            return jsonify(error="Voice session not found"), 404
        if row["ended_at"] is None:
            elapsed = max(0, int((now - (datetime.fromisoformat(row["started_at"]) if isinstance(row["started_at"],str) else row["started_at"])).total_seconds()))
            charged = min(row["reserved_seconds"], elapsed)
            db.session.execute(sql_text("""UPDATE voice_quota_sessions
                SET ended_at=:now, charged_seconds=:charged
                WHERE session_id=:sid AND ended_at IS NULL"""),
                {"now":now,"charged":charged,"sid":sid})
        db.session.commit()
        return jsonify(ok=True)
    except (BadSignature, ValueError, TypeError, AttributeError):
        db.session.rollback()
        return jsonify(error="Invalid voice session"), 403
    except Exception:
        db.session.rollback()
        app.logger.exception("Voice finish failed")
        return jsonify(error="Could not finalize voice allowance"), 503


@app.route("/api/voice/session", methods=["POST"])
@limiter.limit("10 per hour")
@login_required
def create_live_voice_session():
    if not current_user.email_verified:
        return jsonify(error="Email verification required"), 403
    if os.getenv("LIVE_VOICE_ENABLED", "false").lower() != "true":
        return jsonify(error="Live Voice is not enabled"), 503
    test_user_id = os.getenv("LIVE_VOICE_TEST_USER_ID", "").strip()
    if not test_user_id or str(current_user.id) != test_user_id:
        return jsonify(error="Live Voice beta is restricted to a test account"), 403
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return jsonify(error="Voice service unavailable"), 503

    data = request.get_json(silent=True) or {}
    try:
        mode, context, instructions = _voice_context(data)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    # Check quota before requesting a paid Realtime client secret.
    try:
        quota = _voice_quota_snapshot(current_user.id)
    except Exception:
        return jsonify(error="Voice allowance temporarily unavailable"), 503
    if quota["remaining_seconds"] <= 0:
        return jsonify(error="Daily voice allowance exhausted. Try again after 00:00 UTC.",
                       quota=quota), 429

    import urllib.request
    import urllib.error
    payload = {
        "expires_after": {"anchor": "created_at", "seconds": 30},
        "session": {
            "type": "realtime",
            "model": VOICE_MODEL,
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "output": {"voice": os.getenv('KHAYYAM_VOICE','cedar')},
                "input": {
                    "transcription": {"model": os.getenv('REALTIME_TRANSCRIBE_MODEL','gpt-live-transcribe'),
                                      "prompt": "The speaker may use Afghan Dari (دری افغانستان), Persian script, and occasional English words. Transcribe the words actually spoken; do not translate or invent text."},
                    "turn_detection": {
                        "type": "server_vad", "create_response": True,
                        "interrupt_response": True
                    }
                }
            }
        }
    }
    user_hash = hashlib.sha256(f"khayyam:{current_user.id}".encode()).hexdigest()
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "OpenAI-Safety-Identifier": user_hash},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
        # Persist the session before issuing a receipt; all batches use one conversation.
        _ensure_voice_storage()
        # Single atomic daily+monthly reservation, shared across all voice features.
        now=_voice_now()
        session_id,quota=_reserve_voice_seconds(current_user.id,
            min(VOICE_PLANS[active_voice_tier(current_user)]['daily'],
                quota['remaining_seconds']),kind='live')
        if not session_id:
            return jsonify(error='Voice allowance exhausted or another session active',quota=quota),429
        try:
            db.session.execute(sql_text('''INSERT INTO voice_save_sessions
                (session_id,user_id,mode,conversation_id)
                VALUES(:sid,:uid,:mode,:cid)'''),
                {'sid':session_id,'uid':current_user.id,'mode':mode,
                 'cid':context.get('conversation_id') if mode=='chat' else None})
            db.session.commit()
        except Exception:
            db.session.rollback()
            # Cancel reservation locally if no session receipt is issued.
            db.session.execute(sql_text('DELETE FROM voice_quota_sessions WHERE session_id=:sid'),
                               {'sid':session_id})
            db.session.commit()
            raise
        remaining=min(VOICE_PLANS[active_voice_tier(current_user)]['daily'],
                      quota['remaining_seconds'])
        receipt = URLSafeSerializer(app.secret_key, salt="khayyam-voice-v2").dumps(
            {"uid": current_user.id, "mode": mode, "context": context,
             "sid": session_id, "issued": int(time.time())})
        return jsonify(value=result["value"], expires_at=result.get("expires_at"),
                       receipt=receipt, mode=mode, allowance_seconds=remaining,
                       warning_seconds=VOICE_WARNING_SECONDS,
                       monthly_remaining_seconds=quota['monthly_remaining_seconds'],
                       allowance_deadline=int(now.replace(tzinfo=timezone.utc).timestamp())+remaining)
    except urllib.error.HTTPError as exc:
        app.logger.error("Realtime session creation failed: HTTP %s", exc.code)
        return jsonify(error="Could not start voice session"), 502
    except Exception:
        app.logger.exception("Realtime session creation failed")
        return jsonify(error="Voice service temporarily unavailable"), 503


@app.route("/api/voice/save", methods=["POST"])
@limiter.limit("30 per minute")
@login_required
def save_live_voice_transcript():
    """Idempotent, session-bound voice saves, safe to retry after lost responses."""
    if not current_user.email_verified:
        return jsonify(error="Email verification required"), 403
    data = request.get_json(silent=True) or {}
    try:
        receipt = URLSafeSerializer(app.secret_key, salt="khayyam-voice-v2").loads(
            str(data.get("receipt") or ""))
        issued = int(receipt.get("issued", 0))
        if receipt.get("uid") != current_user.id or not 0 <= time.time() - issued <= 7200:
            return jsonify(error="Voice session expired"), 403
        sid = str(receipt.get("sid") or "")
        if not re.fullmatch(r"[0-9a-f-]{36}", sid):
            return jsonify(error="Unsupported voice session; please restart voice"), 403
        mode = receipt["mode"]
        context = receipt["context"]
    except (BadSignature, ValueError, TypeError, KeyError, AttributeError):
        return jsonify(error="Invalid voice session"), 403

    turns = data.get("turns", [])
    if not isinstance(turns, list) or len(turns) > 30:
        return jsonify(error="Invalid transcript"), 400
    clean = []
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            return jsonify(error="Invalid transcript turn"), 400
        content, turn_id = turn.get("content"), turn.get("turn_id")
        if not isinstance(content, str) or len(content) > 5000 or not isinstance(turn_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", turn_id):
            return jsonify(error="Invalid transcript content or turn ID"), 400
        if content.strip():
            clean.append({"role": turn["role"], "content": content.strip(), "turn_id": turn_id})
    try:
        _ensure_voice_storage()
        # PostgreSQL row lock serializes concurrent saves for the same session.
        lock = " FOR UPDATE" if db.engine.dialect.name == "postgresql" else ""
        row = db.session.execute(sql_text("""SELECT session_id, user_id, mode, conversation_id
            FROM voice_save_sessions WHERE session_id = :sid""" + lock), {"sid": sid}).mappings().first()
        if not row or row["user_id"] != current_user.id or row["mode"] != mode:
            db.session.rollback()
            return jsonify(error="Voice session not found"), 403
        conv_id = row["conversation_id"]
        if mode == "chat":
            conv = Conversation.query.filter_by(id=conv_id, user_id=current_user.id).first() if conv_id else None
            if conv_id and not conv:
                db.session.rollback()
                return jsonify(error="Conversation not found"), 404
        elif mode == "tutor":
            subject, topic_key, level = context["subject"], context["topic_key"], context["level"]
            if subject not in CURRICULUM or not any(t["key"] == topic_key for t in CURRICULUM[subject]["levels"].get(level, {}).get("topics", [])):
                db.session.rollback()
                return jsonify(error="Lesson not found"), 400
        else:
            db.session.rollback()
            return jsonify(error="Invalid mode"), 400

        # Claim each turn ID inside the same transaction as its message insert.
        new_turns = []
        for turn in clean:
            exists = db.session.execute(sql_text("""SELECT 1 FROM voice_saved_turns
                WHERE session_id=:sid AND turn_id=:tid"""), {"sid": sid, "tid": turn["turn_id"]}).first()
            if exists:
                continue
            db.session.execute(sql_text("""INSERT INTO voice_saved_turns (session_id, turn_id)
                VALUES (:sid, :tid)"""), {"sid": sid, "tid": turn["turn_id"]})
            new_turns.append(turn)

        if mode == "chat":
            if new_turns and not conv:
                title = next((t["content"] for t in new_turns if t["role"] == "user"), "گفتگوی صوتی")
                conv = Conversation(user_id=current_user.id, title=title[:60])
                db.session.add(conv)
                db.session.flush()
                db.session.execute(sql_text("""UPDATE voice_save_sessions
                    SET conversation_id=:cid WHERE session_id=:sid"""), {"cid": conv.id, "sid": sid})
            if new_turns:
                for turn in new_turns:
                    db.session.add(Message(conversation_id=conv.id, role=turn["role"], content=turn["content"]))
                conv.updated_at = datetime.utcnow()
            result = {"saved": len(new_turns), "conversation_id": conv.id if conv else None, "mode": mode}
        else:
            if new_turns:
                progress = TutorProgress.query.filter_by(user_id=current_user.id, subject=subject).first()
                if not progress:
                    progress = TutorProgress(user_id=current_user.id, subject=subject)
                    db.session.add(progress)
                    db.session.flush()
                history = _lesson_history(current_user.id, subject, topic_key, lock=True)
                history.extend({"role": t["role"], "content": t["content"]} for t in new_turns)
                _save_lesson_history(current_user.id, subject, topic_key, history)
                progress.chat_history = json.dumps(history[-60:], ensure_ascii=False)
                progress.last_topic_title = _topic_info(subject, level, topic_key)['title']
                progress.last_activity = datetime.utcnow()
            result = {"saved": len(new_turns), "mode": mode}
        db.session.commit()
        return jsonify(result)
    except Exception:
        db.session.rollback()
        app.logger.exception("Voice transcript save failed")
        return jsonify(error="Voice save failed; please retry"), 503


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "").lower() == "true")