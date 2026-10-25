"""Khayyam's cost guard.

Two independent protections, both enforced BEFORE a provider call is made:

1. Token windows per tier (what the pricing page promises users). Users are charged
   for what they and the model write (history + message + answer), NOT for Khayyam's
   own fixed system prompt, so a plan limit is never eaten by our overhead.
2. A USD ceiling per owner per month (what protects us). It is computed from the
   provider's real usage numbers and includes the system prompt.

Hot path = 4 SQL statements (advisory lock, one aggregate read, one insert, one update).
Schema creation happens once at startup (ensure_storage), never per request.
Failed provider calls (HTTP errors, no answer) are refunded; only calls that may have
been billed (timeouts) are charged, and only conservatively.
"""
import os
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import text, inspect

VOICE_PLANS = {
    # The dollar caps below decide how much a student can talk. These seconds are only a second, much looser
    # safety net (in case a cost report ever goes missing): generous, but never unlimited.
    'free': {'price': 0, 'daily': 900, 'monthly': 5400},
    'basic': {'price': 15, 'daily': 2400, 'monthly': 21600},
    'pro': {'price': 30, 'daily': 4500, 'monthly': 45000},
    'premium': {'price': 60, 'daily': 7200, 'monthly': 72000},
    'tutor_pro': {'price': 90, 'daily': 10800, 'monthly': 108000},
}
SESSION_MAX_SECONDS = int(os.getenv('VOICE_SESSION_MAX_SECONDS', '2400'))      # one live session never runs longer than this
# The real voice ceiling: USD of live voice, dictation and read-aloud per calendar month (UTC), measured
# from what OpenAI reports (voice_usage). The seconds above stay as a second, stricter guard.
# Override one plan with VOICE_USD_CAPS_JSON, e.g. {"basic": 5}.
VOICE_USD_CAPS = {'free': 1.0, 'basic': 4.0, 'pro': 10.0, 'premium': 15.0, 'tutor_pro': 25.0}
# Daily dollar caps (UTC day): every plan except Tutor Pro, which only has the monthly cap. None = no daily cap.
VOICE_USD_DAILY_CAPS = {'free': .25, 'basic': .80, 'pro': 2.0, 'premium': 3.0, 'tutor_pro': None}


def voice_usd_daily_cap(plan):
    import json
    cap = VOICE_USD_DAILY_CAPS.get(plan, VOICE_USD_DAILY_CAPS['free'])
    try:
        extra = json.loads(os.getenv('VOICE_USD_DAILY_CAPS_JSON', '') or '{}')
        if isinstance(extra, dict) and plan in extra:
            cap = None if extra[plan] in (None, 0, '0') else float(extra[plan])
    except (ValueError, TypeError):
        pass
    return cap


def voice_usd_cap(plan):
    import json
    cap = VOICE_USD_CAPS.get(plan, VOICE_USD_CAPS['free'])
    try:
        extra = json.loads(os.getenv('VOICE_USD_CAPS_JSON', '') or '{}')
        if isinstance(extra, dict) and plan in extra:
            cap = float(extra[plan])
    except (ValueError, TypeError):
        pass
    return cap
# Separate monthly API cost ceilings in USD, NOT token counts; adjust only with evidence.
USD_CAPS = {'free': {'chat': 1.50, 'tutor': .70},
            'basic': {'chat': 3.0, 'tutor': 1.50},
            'pro': {'chat': 5.0, 'tutor': 2.0},
            'premium': {'chat': 10.0, 'tutor': 5.0},
            'tutor_pro': {'chat': 1.50, 'tutor': 23.0}}
# USD per million billed tokens. Override by env on price changes.
TOKEN_RATES = {
    'gpt-5.4-mini': (.75, 4.50), 'gpt-5.4-nano': (.20, 1.25),
    'gpt-5.4': (2.50, 15.0), 'openai/gpt-oss-20b': (.075, .30),
    'openai/gpt-oss-120b': (.15, .60),
}


# =============================================================================================
# GLOBAL CIRCUIT BREAKER: whatever goes wrong (a bug, a loop, an attack), the whole service stops spending
# paid API money for the rest of the UTC day once everyone together has spent this much. It protects the
# bill, not one user. GLOBAL_DAILY_USD_LIMIT=0 switches it off (do not).
# =============================================================================================
GLOBAL_DAILY_USD_LIMIT = float(os.getenv('GLOBAL_DAILY_USD_LIMIT', '100'))
_BREAKER = {'at': 0.0, 'micro': 0}


def global_spend_today_micro(db, fresh=False):
    """Everything paid for today by all users together (text models, voice, tools). Cached for 20 seconds.
    Raises when it cannot be computed (callers fail CLOSED)."""
    import time as _t
    if not fresh and _t.time() - _BREAKER['at'] < 20:
        return _BREAKER['micro']
    now = utc_now(); start = now.replace(hour=0, minute=0, second=0, microsecond=0); day = now.strftime('%Y-%m-%d')
    total = 0
    total += int(db.session.execute(text('''SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_micro
        ELSE actual_micro END),0) FROM api_usage_requests WHERE created_at>=:s'''), {'s': start}).scalar() or 0)
    total += int(db.session.execute(text('''SELECT COALESCE(SUM(est_micro),0) FROM api_tool_usage
        WHERE created_at>=:s AND status!='failed' '''), {'s': start}).scalar() or 0)
    try:
        total += int(db.session.execute(text('SELECT COALESCE(SUM(micro),0) FROM voice_usage WHERE usage_day=:d'), {'d': day}).scalar() or 0)
    except Exception:
        db.session.rollback()          # voice table not created yet (nothing spent on voice)
    _BREAKER.update(at=_t.time(), micro=total)
    return total


def breaker_open(db):
    """True = stop spending. Also True when the total cannot be read (fail closed)."""
    limit = float(os.getenv('GLOBAL_DAILY_USD_LIMIT', GLOBAL_DAILY_USD_LIMIT))
    if limit <= 0:
        return False
    try:
        return global_spend_today_micro(db) >= int(limit * 1e6)
    except Exception:
        try: db.session.rollback()
        except Exception: pass
        return True


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def chat_plan(user):
    if user is None: return 'free'
    p = getattr(user, 'chat_plan', None) or getattr(user, 'plan', 'free') or 'free'
    exp = getattr(user, 'chat_plan_expires_at', None)
    if p != 'free' and exp is not None and exp.replace(tzinfo=None) <= utc_now(): return 'free'
    return p if p in ('free','basic','pro','premium') else 'free'


def tutor_plan(user):
    if user is None: return 'free'
    p = getattr(user, 'tutor_plan', 'free') or 'free'
    exp = getattr(user, 'tutor_plan_expires_at', None)
    if p != 'free' and exp is not None and exp.replace(tzinfo=None) <= utc_now(): return 'free'
    return p if p == 'tutor_pro' else 'free'


def tutor_tier(user):
    # Retain pre-existing standard Tutor entitlement from Chat plan;
    # independent Tutor Pro overrides only when separately active.
    return 'tutor_pro' if tutor_plan(user) == 'tutor_pro' else chat_plan(user)


def voice_tier(user):
    # A single pooled voice allowance. Never add Chat and Tutor minutes.
    return 'tutor_pro' if tutor_plan(user) == 'tutor_pro' else chat_plan(user)


def rates(model):
    if model not in TOKEN_RATES:
        raise ValueError('No trusted price configuration for '+model)
    default = TOKEN_RATES[model]
    key = ''.join(x if x.isalnum() else '_' for x in model).upper()
    return tuple(float(os.getenv('PRICE_'+key+'_'+s, v)) for s,v in zip(('INPUT','OUTPUT'), default))


def usd_micro(model, prompt_tokens, completion_tokens):
    inp,out = rates(model)
    return int(round(max(0,prompt_tokens)*inp + max(0,completion_tokens)*out))
    # 1 USD/1M tokens = 1 microUSD/token.


def ensure_storage(db):
    """Idempotent. Call once at startup. Works on PostgreSQL and SQLite."""
    with db.engine.begin() as conn:
        conn.execute(text('''CREATE TABLE IF NOT EXISTS api_usage_requests (
            request_id VARCHAR(36) PRIMARY KEY,
            owner_key VARCHAR(160) NOT NULL,
            feature VARCHAR(16) NOT NULL, tier INTEGER NOT NULL,
            model VARCHAR(100) NOT NULL, provider VARCHAR(16) NOT NULL,
            window_key VARCHAR(12) NOT NULL, month_key VARCHAR(7) NOT NULL,
            reserved_tokens INTEGER NOT NULL, reserved_micro INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            actual_micro INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(12) NOT NULL DEFAULT 'reserved',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)'''))
        conn.execute(text('''CREATE INDEX IF NOT EXISTS ix_api_owner_scope
            ON api_usage_requests (owner_key,feature,month_key)'''))
        conn.execute(text('''CREATE INDEX IF NOT EXISTS ix_api_created
            ON api_usage_requests (created_at)'''))
    # Diagnostic columns (added once; harmless if they already exist).
    cols = {c['name'] for c in inspect(db.engine).get_columns('api_usage_requests')}
    extra = {'intent': 'VARCHAR(24)', 'latency_ms': 'INTEGER', 'overhead_tokens': 'INTEGER',
             'total_ms': 'INTEGER', 'db_ms': 'INTEGER', 'db_queries': 'INTEGER'}
    with db.engine.begin() as conn:
        for name, typ in extra.items():
            if name not in cols:
                conn.execute(text(f'ALTER TABLE api_usage_requests ADD COLUMN {name} {typ}'))
        if db.engine.dialect.name == 'postgresql':
            # An earlier build created `intent` as VARCHAR(16); labels like "standard/longform" need more.
            conn.execute(text('ALTER TABLE api_usage_requests ALTER COLUMN intent TYPE VARCHAR(24)'))


def lock_owner(db, owner):
    """Serialize concurrent reservations for one owner. One statement on PostgreSQL;
    a no-op on SQLite (single-process development)."""
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:o))'), {'o': owner})


def owner_key(uid, guest_key=None):
    return 'u:'+str(uid) if uid is not None else 'g:'+str(guest_key or 'anonymous')[:110]


def budget_micro(plan, feature, owner):
    if owner.startswith('g:'): return int(float(os.getenv('GUEST_USD_PER_MONTH','0.10'))*1e6)
    return int(USD_CAPS.get(plan, USD_CAPS['free'])[feature]*1e6)


def _window(period, now):
    return now.strftime('%Y-%m-%d' if period == 'daily' else '%Y-%m')


def next_reset_ts(period, now=None):
    """UTC unix timestamp at which a 'daily' or 'monthly' window rolls over."""
    now = now or utc_now()
    if period == 'daily':
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = nxt.replace(year=nxt.year + 1, month=1) if nxt.month == 12 else nxt.replace(month=nxt.month + 1)
    return int(nxt.replace(tzinfo=timezone.utc).timestamp())


def guest_global_spent_micro(db):
    """All guests combined, today (UTC). Stops a many-IP flood from draining the budget."""
    now = utc_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(db.session.execute(text('''SELECT COALESCE(SUM(CASE WHEN status='reserved'
        THEN reserved_micro ELSE actual_micro END),0) FROM api_usage_requests
        WHERE owner_key LIKE 'g:%' AND created_at>=:s'''), {'s': start}).scalar_one() or 0)


def reserve(db, uid, feature, plan, options, estimated_input, max_output=900,
            guest_key=None, overhead_tokens=0, intent=None):
    """Returns (reservation, index into `options`) or (None, None).

    options: ordered by PREFERENCE. Each: {'model','limit','reset':'daily'|'monthly','tier'}
    estimated_input: all input tokens we will send (system prompt included) -> USD guard.
    overhead_tokens: the part of estimated_input that is Khayyam's own fixed prompt;
                     it is NOT charged against the user's token window.
    """
    assert feature in ('chat','tutor')
    if breaker_open(db):
        return None, None
    now = utc_now(); owner=owner_key(uid, guest_key); month=now.strftime('%Y-%m')
    estimated_input = max(1, min(int(estimated_input), 40000))
    overhead_tokens = max(0, min(int(overhead_tokens), estimated_input - 1))
    charged_input = estimated_input - overhead_tokens
    max_output = max(1, min(int(max_output), 4000))
    lock_owner(db, owner)
    rows = db.session.execute(text('''SELECT tier, window_key,
          COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_tokens
                       ELSE input_tokens+output_tokens END),0) AS toks,
          COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_micro
                       ELSE actual_micro END),0) AS micro
        FROM api_usage_requests
        WHERE owner_key=:o AND feature=:f AND month_key=:m
        GROUP BY tier, window_key'''), {'o':owner,'f':feature,'m':month}).all()
    spent = sum(int(r[3] or 0) for r in rows)
    used_by = {(int(r[0]), r[1]): int(r[2] or 0) for r in rows}
    budget = budget_micro(plan, feature, owner)
    for ix, choice in enumerate(options):
        if not choice.get('limit') or choice.get('reset') not in ('daily','monthly'): continue
        model=choice['model']; provider='groq' if model.startswith('openai/gpt-oss') else 'openai'
        period = choice['reset']; window=_window(period,now); tier=int(choice['tier'])
        used = used_by.get((tier, window), 0)
        remaining = int(choice['limit']) - used - charged_input
        allocated_output = min(max_output, remaining)
        if allocated_output < 120:
            continue
        reserve_cost = usd_micro(model, estimated_input, allocated_output)
        if spent + reserve_cost > budget:
            continue
        rid=str(uuid.uuid4())
        db.session.execute(text('''INSERT INTO api_usage_requests
          (request_id,owner_key,feature,tier,model,provider,window_key,month_key,
           reserved_tokens,reserved_micro,intent,overhead_tokens)
          VALUES (:id,:o,:f,:t,:model,:provider,:w,:m,:rt,:rc,:intent,:oh)'''),
          {'id':rid,'o':owner,'f':feature,'t':tier,'model':model,'provider':provider,
           'w':window,'m':month,'rt':charged_input+allocated_output,'rc':reserve_cost,
           'intent':(intent or '')[:16] or None,'oh':overhead_tokens})
        db.session.commit()
        return {'id':rid,'model':model,'tier':tier,'feature':feature,'owner':owner,
                'max_output':allocated_output,'overhead':overhead_tokens,'intent':intent}, ix
    db.session.commit()
    return None,None


def finalize(db, reservation, input_tokens=None, output_tokens=None,
             uncertain=False, failed=False, latency_ms=None):
    """Settle a reservation. One UPDATE; guarded so it can only settle a 'reserved' row.

    done      -> user charged (input - fixed overhead) + output; USD from real counts
    failed    -> refunded entirely (provider returned an error / nothing to bill)
    uncertain -> charged at the reservation (request may have been billed, e.g. timeout)
    """
    if not reservation: return
    rid = reservation['id']
    if failed:
        db.session.execute(text('''UPDATE api_usage_requests SET status='failed',
            actual_micro=0,input_tokens=0,output_tokens=0,latency_ms=:ms
            WHERE request_id=:id AND status='reserved' '''),{'id':rid,'ms':latency_ms})
    elif uncertain:
        db.session.execute(text('''UPDATE api_usage_requests SET status='uncertain',
          actual_micro=reserved_micro, input_tokens=reserved_tokens, output_tokens=0,
          latency_ms=:ms WHERE request_id=:id AND status='reserved' '''),{'id':rid,'ms':latency_ms})
    else:
        i=max(0,int(input_tokens or 0)); o=max(0,int(output_tokens or 0))
        actual=usd_micro(reservation['model'],i,o)
        # The fixed system prompt's share of the REAL input count (by characters), so the
        # user is never over- or under-credited by our own token estimate.
        share=reservation.get('sys_share')
        overhead=int(round(i*share)) if share is not None else int(reservation.get('overhead') or 0)
        overhead=max(0,min(overhead,i))
        charged_in=i-overhead
        db.session.execute(text('''UPDATE api_usage_requests SET status='done',
            actual_micro=:cost,input_tokens=:input,output_tokens=:output,latency_ms=:ms,
            overhead_tokens=:oh
            WHERE request_id=:id AND status='reserved' '''),
            {'cost':actual,'input':charged_in,'output':o,'ms':latency_ms,'oh':overhead,'id':rid})
    db.session.commit()


def recent_requests(db, limit=60):
    """Newest first, for the admin diagnostics view."""
    rows = db.session.execute(text('''SELECT created_at, owner_key, feature, intent, model, status,
        reserved_tokens, input_tokens, output_tokens, overhead_tokens, actual_micro, latency_ms,
        total_ms, db_ms, db_queries
        FROM api_usage_requests ORDER BY created_at DESC LIMIT :n'''),{'n':int(limit)}).mappings().all()
    return [{
        'at': r['created_at'].isoformat() if hasattr(r['created_at'],'isoformat') else str(r['created_at']),
        'owner': r['owner_key'][:6]+'…' if r['owner_key'].startswith('g:') else r['owner_key'],
        'feature': r['feature'], 'intent': r['intent'], 'model': r['model'], 'status': r['status'],
        'reserved': r['reserved_tokens'], 'charged_in': r['input_tokens'], 'out': r['output_tokens'],
        'overhead': r['overhead_tokens'], 'usd': round(int(r['actual_micro'] or 0)/1e6, 5),
        'ms': r['latency_ms'], 'total_ms': r['total_ms'], 'db_ms': r['db_ms'],
        'db_queries': r['db_queries']} for r in rows]


def summary(db, uid):
    owner=owner_key(uid)
    rows=db.session.execute(text('''SELECT feature,model,provider,
      SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
      SUM(CASE WHEN status='reserved' THEN reserved_micro ELSE actual_micro END) AS micro_usd
      FROM api_usage_requests WHERE owner_key=:o AND month_key=:m GROUP BY feature,model,provider'''),
      {'o':owner,'m':utc_now().strftime('%Y-%m')}).mappings().all()
    return [{'feature':r['feature'],'model':r['model'],'provider':r['provider'],
       'input_tokens':int(r['input_tokens'] or 0),'output_tokens':int(r['output_tokens'] or 0),
       'estimated_usd':round(int(r['micro_usd'] or 0)/1e6,5)} for r in rows]


def tier_summary(db, uid, feature, options):
    now=utc_now();owner=owner_key(uid)
    summary=[]
    for pos,spec in enumerate(options):
        period=spec['reset']; window=_window(period,now); tier=pos+1
        row=db.session.execute(text("""SELECT
            COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_tokens
                 ELSE input_tokens+output_tokens END),0) AS used
            FROM api_usage_requests WHERE owner_key=:owner AND feature=:feature
              AND tier=:tier AND window_key=:window"""),
            {'owner':owner,'feature':feature,'tier':tier,'window':window}).mappings().first()
        used=int(row['used'] or 0)
        limit=int(spec['limit'])
        # Display the real UTC rollover used by `_window`, not a null reset date.
        if period == 'daily':
            nxt = (now + timedelta(days=1)).replace(hour=0, minute=0,
                                                    second=0, microsecond=0)
        else:
            nxt = now.replace(day=1, hour=0, minute=0, second=0,
                              microsecond=0)
            if nxt.month == 12:
                nxt = nxt.replace(year=nxt.year + 1, month=1)
            else:
                nxt = nxt.replace(month=nxt.month + 1)
        summary.append({'model':spec['model'],'used':used,'limit':limit,
                        'period':period,'remaining':max(0,limit-used),
                        'reset_ts':int(nxt.replace(tzinfo=timezone.utc).timestamp())})
    return summary


# =============================================================================================
# TOOL QUOTAS (pictures, web searches, documents)
# =============================================================================================
# Counts per calendar month, plus a USD ceiling for all tools together. PROVISIONAL numbers: they
# are meant to be tuned from real usage (admin log) and the plan prices. Override without a code
# change with the TOOL_PLANS_JSON environment variable, e.g. {"basic": {"image": 40}}.
# 'image' is the total number of pictures (standard 1K and HD 2K together); 'image_hd' is how many of
# those may be 2K. The USD ceiling covers everything, so HD pictures can never push the cost past it.
TOOL_PLANS = {
    'free':    {'image': 3,   'image_hd': 0,  'search': 4,   'doc': 3,   'usd': .25},
    'basic':   {'image': 20,  'image_hd': 0,  'search': 25,  'doc': 40,  'usd': 1.60},
    'pro':     {'image': 50,  'image_hd': 10, 'search': 60,  'doc': 120, 'usd': 4.00},
    'premium': {'image': 120, 'image_hd': 30, 'search': 130, 'doc': 300, 'usd': 9.00},
    # Tutor Pro: the same tools as Pro (the tutor can search the web and make pictures too)
    'tutor_pro': {'image': 50,  'image_hd': 10, 'search': 60,  'doc': 120, 'usd': 4.00},
}


def tool_tier(user):
    """Which row of TOOL_PLANS a user gets: their chat plan, or the Tutor Pro row when that is better."""
    cp = chat_plan(user)
    return 'tutor_pro' if tutor_plan(user) == 'tutor_pro' and cp in ('free', 'basic') else cp
# Estimated provider cost of one use, in micro-USD (1 USD = 1,000,000). Documents are made on our
# own server, so they cost nothing per use. Checked against Google's price list on Oct 25 2026:
# picture 1K $0.0336 / 2K $0.0504 (Nano Banana 2.1); a web search is about $0.014 per Google query
# (usually two) plus the answer text, so $0.035 covers it with room.
TOOL_COST_MICRO = {'image': 35000, 'image_hd': 52000, 'search': 35000, 'doc': 0, 'overlay': 0}


def tool_plan(plan):
    import json
    cfg = dict(TOOL_PLANS.get(plan, TOOL_PLANS['free']))
    try:
        extra = json.loads(os.getenv('TOOL_PLANS_JSON', '') or '{}')
        cfg.update(extra.get(plan, {}) if isinstance(extra, dict) else {})
    except ValueError:
        pass
    return cfg


def tool_cost_micro(kind):
    return int(float(os.getenv('TOOL_COST_%s_MICRO' % kind.upper(), TOOL_COST_MICRO.get(kind, 0))))


def search_cost_micro(n_queries):
    """Real cost of one grounded search: the Google fee per query that actually ran, plus the answer text.
    Never below the flat estimate, so a cheap search cannot make the ceiling too generous."""
    per_query = int(float(os.getenv('TOOL_SEARCH_QUERY_MICRO', '14000')))
    text_part = int(float(os.getenv('TOOL_SEARCH_TEXT_MICRO', '7000')))
    return max(tool_cost_micro('search'), max(1, int(n_queries)) * per_query + text_part)


def hd_left(db, uid, plan):
    """True when this plan may still make a 2K picture this month (read only; the reservation re-checks)."""
    cfg = tool_plan(plan)
    if int(cfg.get('image_hd', 0)) <= 0:
        return False
    owner = owner_key(uid)
    month = utc_now().strftime('%Y-%m')
    rows = db.session.execute(text('''SELECT kind, COUNT(*) FROM api_tool_usage
        WHERE owner_key=:o AND month_key=:m AND status!='failed' AND kind IN ('image','image_hd') GROUP BY kind'''),
        {'o': owner, 'm': month}).all()
    used = {r[0]: int(r[1]) for r in rows}
    return used.get('image_hd', 0) < int(cfg['image_hd']) and used.get('image', 0) + used.get('image_hd', 0) < int(cfg['image'])


def ensure_tool_storage(db):
    with db.engine.begin() as conn:
        conn.execute(text('''CREATE TABLE IF NOT EXISTS api_tool_usage (
            request_id VARCHAR(36) PRIMARY KEY,
            owner_key VARCHAR(160) NOT NULL,
            kind VARCHAR(12) NOT NULL,
            month_key VARCHAR(7) NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'reserved',
            est_micro INTEGER NOT NULL DEFAULT 0,
            detail VARCHAR(120),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)'''))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_tool_owner ON api_tool_usage (owner_key, month_key, kind)'))


def tool_reserve(db, uid, kind, plan, detail=None):
    """Returns (request_id, None) when allowed, or (None, reason) with reason 'count' | 'usd' | 'hd' | 'breaker'.
    Documents and text overlays are counted but never blocked by the USD ceiling (they cost nothing)."""
    if tool_cost_micro(kind) and breaker_open(db):
        return None, 'breaker'
    owner = owner_key(uid)
    cfg = tool_plan(plan)
    cost = tool_cost_micro(kind)
    month = utc_now().strftime('%Y-%m')
    lock_owner(db, owner)
    rows = db.session.execute(text('''SELECT kind, COUNT(*) AS n, COALESCE(SUM(est_micro),0) AS micro
        FROM api_tool_usage WHERE owner_key=:o AND month_key=:m AND status!='failed' GROUP BY kind'''),
        {'o': owner, 'm': month}).all()
    used = {r[0]: int(r[1]) for r in rows}
    spent = sum(int(r[2]) for r in rows)
    if kind in ('image', 'image_hd'):
        if used.get('image', 0) + used.get('image_hd', 0) >= int(cfg['image']):
            db.session.commit()
            return None, 'count'
        if kind == 'image_hd' and used.get('image_hd', 0) >= int(cfg.get('image_hd', 0)):
            db.session.commit()
            return None, 'hd'
    elif kind in cfg and used.get(kind, 0) >= int(cfg[kind]):
        db.session.commit()
        return None, 'count'
    if cost and spent + cost > int(float(cfg.get('usd', 0)) * 1e6):
        db.session.commit()
        return None, 'usd'
    rid = str(uuid.uuid4())
    db.session.execute(text('''INSERT INTO api_tool_usage (request_id, owner_key, kind, month_key, est_micro, detail)
        VALUES (:id, :o, :k, :m, :c, :d)'''), {'id': rid, 'o': owner, 'k': kind, 'm': month, 'c': cost,
                                               'd': (detail or '')[:120] or None})
    db.session.commit()
    return rid, None


def tool_finalize(db, rid, failed=False, cost_micro=None):
    """failed -> refunded (no count, no cost). Otherwise stays charged at the estimate, or at cost_micro
    when the real cost is known (a search that ran more Google queries than usual)."""
    if not rid:
        return
    db.session.execute(text("UPDATE api_tool_usage SET status=:s, est_micro=CASE WHEN :f THEN 0 "
                            "WHEN :c IS NOT NULL THEN :c ELSE est_micro END "
                            "WHERE request_id=:i AND status='reserved'"),
                       {'s': 'failed' if failed else 'done', 'f': bool(failed),
                        'c': None if cost_micro is None else int(cost_micro), 'i': rid})
    db.session.commit()


def tool_usage(db, uid, plan):
    """{'image': {'used': n, 'limit': n}, ...} for the current month (for the profile page / the model)."""
    owner = owner_key(uid)
    month = utc_now().strftime('%Y-%m')
    rows = db.session.execute(text('''SELECT kind, COUNT(*) FROM api_tool_usage
        WHERE owner_key=:o AND month_key=:m AND status!='failed' GROUP BY kind'''), {'o': owner, 'm': month}).all()
    used = {r[0]: int(r[1]) for r in rows}
    cfg = tool_plan(plan)
    out = {k: {'used': used.get(k, 0), 'limit': int(cfg[k])} for k in ('search', 'doc')}
    out['image'] = {'used': used.get('image', 0) + used.get('image_hd', 0), 'limit': int(cfg['image'])}
    out['image_hd'] = {'used': used.get('image_hd', 0), 'limit': int(cfg.get('image_hd', 0))}
    return out


def tool_recent(db, limit=60):
    rows = db.session.execute(text('''SELECT created_at, owner_key, kind, status, est_micro, detail
        FROM api_tool_usage ORDER BY created_at DESC LIMIT :n'''), {'n': int(limit)}).mappings().all()
    return [{'at': r['created_at'].isoformat() if hasattr(r['created_at'], 'isoformat') else str(r['created_at']),
             'owner': r['owner_key'], 'kind': r['kind'], 'status': r['status'],
             'usd': round(int(r['est_micro'] or 0) / 1e6, 5), 'detail': r['detail']} for r in rows]

# =============================================================================================
# VOICE COSTS (live realtime sessions, dictation, read-aloud)
# =============================================================================================
# USD per 1M tokens, from OpenAI's pricing page (checked Oct 25 2026): (input, cached input, output).
# Override one number without a code change: PRICE_RT_<MODEL>_<MODALITY>_<IN|CACHED|OUT>, e.g.
# PRICE_RT_GPT_REALTIME_2_1_AUDIO_IN=32
REALTIME_RATES = {
    'gpt-realtime-2.1':      {'text': (4.00, .40, 24.00), 'audio': (32.00, .40, 64.00), 'image': (5.00, .50, 0.0)},
    'gpt-realtime-2.1-mini': {'text': (.60, .06, 2.40),   'audio': (10.00, .30, 20.00), 'image': (.80, .08, 0.0)},
}
# USD per minute of audio sent to the speech-to-text model.
TRANSCRIBE_PER_MIN = {'gpt-live-transcribe': .017, 'gpt-transcribe': .0045}
# Read-aloud (gpt-4o-mini-tts): $0.60 / 1M text tokens + $12 / 1M audio tokens, about $0.015 per minute of speech.
TTS_PER_MIN = .015
# Used only when a live session ended without reporting its real token counts (tab crashed, offline):
# USD per minute connected. A GUESS until the real numbers in the admin page say otherwise.
# Deliberately HIGH: a session that never reports must not look cheap (about the cost of a 10-20 minute class with no cache).
FALLBACK_USD_PER_MIN = {'gpt-realtime-2.1': .60, 'gpt-realtime-2.1-mini': .20}
# The least a minute of a live session can honestly cost. A report below this (a broken or tampering browser)
# is raised to it, using the SERVER's own clock for the session length.
FLOOR_USD_PER_MIN = {'gpt-realtime-2.1': .06, 'gpt-realtime-2.1-mini': .02}

USAGE_FIELDS = ('in_text', 'in_audio', 'in_image', 'cin_text', 'cin_audio', 'cin_image', 'out_text', 'out_audio', 'responses')


def _rt_rate(model, modality, slot):
    base = REALTIME_RATES.get(model) or REALTIME_RATES['gpt-realtime-2.1']
    key = 'PRICE_RT_%s_%s_%s' % (''.join(x if x.isalnum() else '_' for x in model).upper(), modality.upper(), ('IN', 'CACHED', 'OUT')[slot])
    try:
        return float(os.getenv(key, base[modality][slot]))
    except ValueError:
        return base[modality][slot]


def realtime_cost_micro(model, u):
    """micro-USD of a realtime session from its token counts (1 USD per 1M tokens = 1 micro-USD per token)."""
    total = 0.0
    for m in ('text', 'audio', 'image'):
        inp = max(0, int(u.get('in_' + m, 0) or 0)); cached = min(inp, max(0, int(u.get('cin_' + m, 0) or 0)))
        out = max(0, int(u.get('out_' + m, 0) or 0))
        total += (inp - cached) * _rt_rate(model, m, 0) + cached * _rt_rate(model, m, 1) + out * _rt_rate(model, m, 2)
    return int(round(total))


def transcribe_cost_micro(model, seconds):
    per_min = float(os.getenv('PRICE_TRANSCRIBE_' + ''.join(x if x.isalnum() else '_' for x in model).upper() + '_PER_MIN', TRANSCRIBE_PER_MIN.get(model, .017)))
    return int(round(max(0, seconds) / 60.0 * per_min * 1e6))


def fallback_live_micro(model, seconds):
    per_min = float(os.getenv('VOICE_FALLBACK_USD_PER_MIN', FALLBACK_USD_PER_MIN.get(model, .25)))
    return int(round(max(0, seconds) / 60.0 * per_min * 1e6))


def floor_live_micro(model, seconds):
    per_min = float(os.getenv('VOICE_FLOOR_USD_PER_MIN', FLOOR_USD_PER_MIN.get(model, .06)))
    return int(round(max(0, seconds) / 60.0 * per_min * 1e6))


def ensure_voice_usage_storage(db):
    with db.engine.begin() as conn:
        conn.execute(text('''CREATE TABLE IF NOT EXISTS voice_usage (
            session_id VARCHAR(36) PRIMARY KEY,
            user_id INTEGER NOT NULL,
            usage_day VARCHAR(10) NOT NULL,
            kind VARCHAR(12) NOT NULL,
            mode VARCHAR(12),
            model VARCHAR(60) NOT NULL,
            estimated INTEGER NOT NULL DEFAULT 0,
            seconds INTEGER NOT NULL DEFAULT 0,
            talk_seconds INTEGER NOT NULL DEFAULT 0,
            in_text BIGINT NOT NULL DEFAULT 0, in_audio BIGINT NOT NULL DEFAULT 0, in_image BIGINT NOT NULL DEFAULT 0,
            cin_text BIGINT NOT NULL DEFAULT 0, cin_audio BIGINT NOT NULL DEFAULT 0, cin_image BIGINT NOT NULL DEFAULT 0,
            out_text BIGINT NOT NULL DEFAULT 0, out_audio BIGINT NOT NULL DEFAULT 0,
            responses INTEGER NOT NULL DEFAULT 0,
            micro INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)'''))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_voice_usage_user_day ON voice_usage (user_id, usage_day)'))


def _clamp_int(v, hi=2_000_000_000):
    try:
        return max(0, min(int(float(v)), hi))
    except (TypeError, ValueError):
        return 0


def record_voice_usage(db, sid, uid, day, kind, model, usage=None, seconds=0, talk_seconds=0, mode=None,
                       estimated=False, micro=None):
    """Insert or update the cost of one voice session. `usage` holds CUMULATIVE totals, so a repeated or
    reordered report can never double count: every counter only ever grows (max of old and new).
    A real report replaces an estimate. micro: fixed cost for dictation / read-aloud."""
    now = utc_now()
    row = db.session.execute(text('SELECT * FROM voice_usage WHERE session_id=:s'), {'s': sid}).mappings().first()
    new = {f: _clamp_int((usage or {}).get(f, 0)) for f in USAGE_FIELDS}
    secs = _clamp_int(seconds, 86400); talk = _clamp_int(talk_seconds, 86400)
    if row:
        if row['estimated'] == 0 and estimated:      # a real report is already there: never overwrite it with a guess
            return int(row['micro'])
        merged = new if (row['estimated'] and not estimated) else {f: max(int(row[f] or 0), new[f]) for f in USAGE_FIELDS}
        secs = max(int(row['seconds'] or 0), secs)
        talk = max(int(row['talk_seconds'] or 0), talk)
        model = row['model'] or model
    else:
        merged = new
    if micro is None:
        if estimated:
            micro = fallback_live_micro(model, secs)
        else:
            tr = os.getenv('REALTIME_TRANSCRIBE_MODEL', 'gpt-live-transcribe')
            micro = realtime_cost_micro(model, merged) + transcribe_cost_micro(tr, talk)
            if kind == 'live':
                micro = max(micro, floor_live_micro(model, secs))
    p = dict(merged); p.update(sid=sid, uid=uid, day=day, kind=kind, mode=mode, model=model, est=1 if estimated else 0,
                               secs=secs, talk=talk, micro=int(micro), now=now)
    if row:
        db.session.execute(text('''UPDATE voice_usage SET estimated=:est, seconds=:secs, talk_seconds=:talk,
            in_text=:in_text, in_audio=:in_audio, in_image=:in_image, cin_text=:cin_text, cin_audio=:cin_audio, cin_image=:cin_image,
            out_text=:out_text, out_audio=:out_audio, responses=:responses, micro=:micro, updated_at=:now WHERE session_id=:sid'''), p)
    else:
        db.session.execute(text('''INSERT INTO voice_usage (session_id,user_id,usage_day,kind,mode,model,estimated,seconds,talk_seconds,
            in_text,in_audio,in_image,cin_text,cin_audio,cin_image,out_text,out_audio,responses,micro,created_at,updated_at)
            VALUES (:sid,:uid,:day,:kind,:mode,:model,:est,:secs,:talk,:in_text,:in_audio,:in_image,:cin_text,:cin_audio,:cin_image,
                    :out_text,:out_audio,:responses,:micro,:now,:now)'''), p)
    db.session.commit()
    return int(micro)


def settle_live_floor(db, sid, uid, day, model, seconds, mode=None):
    """The session is over (finished or found abandoned) after `seconds` by the server's clock: make sure what is
    recorded is at least the floor, and fill in a conservative estimate if nothing was ever reported."""
    row = db.session.execute(text('SELECT micro, estimated FROM voice_usage WHERE session_id=:s'), {'s': sid}).first()
    if not row:
        return record_voice_usage(db, sid, uid, day, 'live', model, seconds=seconds, mode=mode, estimated=True)
    floor = floor_live_micro(model, seconds)
    if int(row[0] or 0) < floor:
        db.session.execute(text('UPDATE voice_usage SET micro=:m, seconds=CASE WHEN seconds<:s THEN :s ELSE seconds END, updated_at=:n WHERE session_id=:i'),
                           {'m': floor, 's': int(seconds), 'n': utc_now(), 'i': sid})
        db.session.commit()
        return floor
    return int(row[0] or 0)


def voice_cost_for_user(db, uid):
    """(today_micro, month_micro) of voice, for one user."""
    day = utc_now().strftime('%Y-%m-%d')
    r = db.session.execute(text('''SELECT COALESCE(SUM(CASE WHEN usage_day=:d THEN micro ELSE 0 END),0),
        COALESCE(SUM(micro),0) FROM voice_usage WHERE user_id=:u AND usage_day LIKE :m'''),
        {'u': uid, 'd': day, 'm': day[:7] + '%'}).first()
    return int(r[0] or 0), int(r[1] or 0)


def cost_history(db, days=30):
    """Everything that burns money, per owner per UTC day, in micro-USD.
    -> {owner_key: {'YYYY-MM-DD': {'chat': n, 'tutor': n, 'voice_live': n, 'voice_other': n, 'tools': n}}}
    Text comes from api_usage_requests, tools from api_tool_usage, voice from voice_usage."""
    days = max(1, min(int(days), 120))
    since = (utc_now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out = {}

    def add(owner, day, key, micro):
        micro = int(micro or 0)
        if not micro: return
        d = out.setdefault(owner, {}).setdefault(str(day)[:10], {'chat': 0, 'tutor': 0, 'voice_live': 0, 'voice_other': 0, 'tools': 0})
        d[key] += micro
    for o, d, f, m in db.session.execute(text('''SELECT owner_key, date(created_at), feature, COALESCE(SUM(actual_micro),0)
            FROM api_usage_requests WHERE created_at>=:s AND status IN ('done','uncertain') GROUP BY owner_key, date(created_at), feature'''), {'s': since}).all():
        add(o, d, 'tutor' if f == 'tutor' else 'chat', m)
    for o, d, m in db.session.execute(text('''SELECT owner_key, date(created_at), COALESCE(SUM(est_micro),0)
            FROM api_tool_usage WHERE created_at>=:s AND status!='failed' GROUP BY owner_key, date(created_at)'''), {'s': since}).all():
        add(o, d, 'tools', m)
    for u, d, k, m in db.session.execute(text('''SELECT user_id, usage_day, kind, COALESCE(SUM(micro),0)
            FROM voice_usage WHERE usage_day>=:s GROUP BY user_id, usage_day, kind'''), {'s': since.strftime('%Y-%m-%d')}).all():
        add('u:%s' % u, d, 'voice_live' if k == 'live' else 'voice_other', m)
    return out


def voice_recent(db, limit=40):
    rows = db.session.execute(text('''SELECT created_at, user_id, kind, mode, model, estimated, seconds, talk_seconds, in_text, in_audio, in_image,
        cin_text, cin_audio, cin_image, out_text, out_audio, responses, micro FROM voice_usage ORDER BY created_at DESC LIMIT :n'''), {'n': int(limit)}).mappings().all()
    out = []
    for r in rows:
        d = dict(r); d['at'] = d.pop('created_at').isoformat() if hasattr(r['created_at'], 'isoformat') else str(r['created_at'])
        d['usd'] = round(int(d.pop('micro') or 0) / 1e6, 4)
        d['per_min'] = round(d['usd'] / (d['seconds'] / 60.0), 3) if d['seconds'] >= 30 else None
        out.append(d)
    return out
