"""Khayyam's cost guard. Additive SQL tables; safe across Render workers via row locks.

All dollar budgets here are protective configuration, not promises of API price.
Only finalized API tokens are recorded as actual usage; reservations cover concurrent calls.
"""
import os
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

VOICE_PLANS = {
    'free': {'price': 0, 'daily': 180, 'monthly': 1800},
    'basic': {'price': 12, 'daily': 300, 'monthly': 5400},
    'pro': {'price': 25, 'daily': 600, 'monthly': 14400},
    'premium': {'price': 49, 'daily': 1200, 'monthly': 27000},
    'tutor_pro': {'price': 69, 'daily': 1800, 'monthly': 45000},
}
# Separate monthly API cost ceilings in USD, NOT token counts; adjust only with evidence.
USD_CAPS = {'free': {'chat': 1.50, 'tutor': .70},
            'basic': {'chat': 3.0, 'tutor': 1.50},
            'pro': {'chat': 5.0, 'tutor': 2.0},
            'premium': {'chat': 19.0, 'tutor': 5.0},
            'tutor_pro': {'chat': 1.50, 'tutor': 23.0}}
# USD per million billed tokens. Override by env on price changes.
TOKEN_RATES = {
    'gpt-5.4-mini': (.75, 4.50), 'gpt-5.4-nano': (.20, 1.25),
    'gpt-5.4': (2.50, 15.0), 'openai/gpt-oss-20b': (.075, .30),
    'openai/gpt-oss-120b': (.15, .60),
}


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
    with db.engine.begin() as conn:
        conn.execute(text('''CREATE TABLE IF NOT EXISTS api_meter_locks (
            owner_key VARCHAR(160) PRIMARY KEY)'''))
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


def lock_owner(db, owner):
    db.session.execute(text('''INSERT INTO api_meter_locks(owner_key) VALUES (:o)
        ON CONFLICT(owner_key) DO NOTHING'''), {'o':owner})
    suffix = ' FOR UPDATE' if db.engine.dialect.name == 'postgresql' else ''
    db.session.execute(text('SELECT owner_key FROM api_meter_locks WHERE owner_key=:o'+suffix), {'o':owner}).one()


def owner_key(uid, guest_key=None):
    return 'u:'+str(uid) if uid is not None else 'g:'+str(guest_key or 'anonymous')[:110]


def budget_micro(plan, feature, owner):
    if owner.startswith('g:'): return int(float(os.getenv('GUEST_USD_PER_MONTH','0.10'))*1e6)
    return int(USD_CAPS.get(plan, USD_CAPS['free'])[feature]*1e6)


def _window(period, now):
    return now.strftime('%Y-%m-%d' if period == 'daily' else '%Y-%m')


def reserve(db, uid, feature, plan, options, estimated_input, max_output=900, guest_key=None):
    """Returns (reservation, config index) or (None,None). Options must have finite limit.

    Each option: {'model':str, 'limit':int, 'reset':'daily'|'monthly','tier':int}
    """
    assert feature in ('chat','tutor')
    now = utc_now(); owner=owner_key(uid, guest_key); month=now.strftime('%Y-%m')
    estimated_input = max(1, min(int(estimated_input), 20000))
    max_output = max(1, min(int(max_output), 4000))
    lock_owner(db,owner)
    # A worker crash leaves a paid reservation outstanding, never auto-refund it.
    db.session.execute(text('''UPDATE api_usage_requests SET status='uncertain',actual_micro=reserved_micro,
        input_tokens=reserved_tokens, output_tokens=0
        WHERE owner_key=:o AND status='reserved' AND created_at<:cutoff'''),
        {'o':owner,'cutoff':now-timedelta(minutes=15)})
    spent = db.session.execute(text('''SELECT COALESCE(SUM(CASE WHEN status='reserved'
        THEN reserved_micro ELSE actual_micro END),0) FROM api_usage_requests
        WHERE owner_key=:o AND feature=:f AND month_key=:m'''),
        {'o':owner,'f':feature,'m':month}).scalar_one()
    for ix, choice in enumerate(options):
        if not choice.get('limit') or choice.get('reset') not in ('daily','monthly'): continue
        model=choice['model']; provider='groq' if model.startswith('openai/gpt-oss') else 'openai'
        period = choice['reset']; window=_window(period,now); tier=int(choice['tier'])
        used = db.session.execute(text('''SELECT COALESCE(SUM(CASE WHEN status='reserved'
            THEN reserved_tokens ELSE input_tokens+output_tokens END),0)
            FROM api_usage_requests WHERE owner_key=:o AND feature=:f AND tier=:t
            AND window_key=:w'''),{'o':owner,'f':feature,'t':tier,'w':window}).scalar_one()
        # Prefer the better model if a smaller but useful response fits.
        # This is a reservation bound, NOT a post-facto token estimate.
        remaining = int(choice['limit']) - int(used) - estimated_input
        allocated_output = min(max_output, remaining)
        if allocated_output < 120:
            continue
        estimate_tokens = estimated_input + allocated_output
        reserve_cost = usd_micro(model, estimated_input, allocated_output)
        if int(spent) + reserve_cost > budget_micro(plan, feature, owner):
            continue
        rid=str(uuid.uuid4())
        db.session.execute(text('''INSERT INTO api_usage_requests
          (request_id,owner_key,feature,tier,model,provider,window_key,month_key,
          reserved_tokens,reserved_micro) VALUES (:id,:o,:f,:t,:model,:provider,:w,:m,:rt,:rc)'''),
          {'id':rid,'o':owner,'f':feature,'t':tier,'model':model,'provider':provider,
           'w':window,'m':month,'rt':estimate_tokens,'rc':reserve_cost})
        db.session.commit()
        return {'id':rid,'model':model,'tier':tier,'feature':feature,
                'owner':owner,'max_output':allocated_output}, ix
    db.session.commit()
    return None,None


def finalize(db, reservation, input_tokens=None, output_tokens=None, uncertain=False):
    if not reservation: return
    owner=reservation['owner']; lock_owner(db,owner)
    if uncertain:
        db.session.execute(text('''UPDATE api_usage_requests SET status='uncertain',
          actual_micro=reserved_micro, input_tokens=reserved_tokens, output_tokens=0
          WHERE request_id=:id AND status='reserved' '''),{'id':reservation['id']})
    else:
        i=max(0,int(input_tokens or 0));o=max(0,int(output_tokens or 0))
        actual=usd_micro(reservation['model'],i,o)
        db.session.execute(text('''UPDATE api_usage_requests SET status='done',
            actual_micro=:cost,input_tokens=:input,output_tokens=:output
            WHERE request_id=:id AND status='reserved' '''),
            {'cost':actual,'input':i,'output':o,'id':reservation['id']})
    db.session.commit()


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
