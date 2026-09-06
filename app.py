from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from database import db, User, Conversation, Message, Memory, UserTokenUsage, SiteConfig, TutorProgress, QuizResult, StudentBadge
from functools import wraps
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
from auth import register_user, login_user_by_username
from datetime import datetime, timezone, timedelta
import os
import json
import re
import base64

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("ADMIN_PASSWORD", "fallback-secret")
database_url = os.getenv("DATABASE_URL", "sqlite:///khayyam.db")
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
groq_client   = Groq(api_key=os.getenv("GROQ_API_KEY"))

KNOWLEDGE_FILE = "knowledge.json"
EXAMPLES_FILE  = "examples.json"

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
        try:
            reply, _ = call_openai_model(
                'gpt-5.4-nano', system_prompt, messages,
                temperature, image_b64, image_type
            )
            return reply, False, None, 'gpt-5.4-nano'
        except Exception as e:
            print(f"Guest OpenAI error: {e}")
            reply, _ = call_groq_model(system_prompt, messages, temperature)
            return reply, False, None, 'llama-4-scout'

    model, tier, reset_ts, switched = pick_model_and_update(
        user_id, plan, estimated_tokens
    )
    print(f"DEBUG: user={user_id} plan={plan} model={model} switched={switched} reset_ts={reset_ts}")

    if 'llama' in model or 'gpt-oss' in model or 'qwen' in model:
        try:
            reply, _ = call_groq_model(system_prompt, messages, temperature)
            return reply, switched, reset_ts, model
        except Exception as e:
            print(f"Groq error: {e}")
            return "متأسفم، سرور مصروف است. لطفاً دوباره امتحان کنید.", switched, reset_ts, model

    try:
        reply, _ = call_openai_model(
            model, system_prompt, messages,
            temperature, image_b64, image_type
        )
        print(f"DEBUG: OpenAI replied with {model}")
        return reply, switched, reset_ts, model
    except Exception as e:
        print(f"OpenAI error with {model}: {e} — falling back to Groq")
        try:
            reply, _ = call_groq_model(system_prompt, messages, temperature)
            return reply, switched, reset_ts, 'llama-4-scout'
        except Exception as e2:
            print(f"Groq fallback failed: {e2}")
            return "متأسفم، در حال حاضر سرور مصروف است. لطفاً چند دقیقه دیگر امتحان کنید.", switched, reset_ts, 'error'

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
def api_register():
    data     = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    email    = (data.get("email") or "").strip() or None
    phone    = (data.get("phone") or "").strip() or None

    if len(username) < 3:
        return jsonify({"success": False, "error": "نام کاربری باید حداقل ۳ حرف باشد"})
    if len(password) < 6:
        return jsonify({"success": False, "error": "رمز عبور باید حداقل ۶ حرف باشد"})

    user, error = register_user(username, password, email, phone)
    if error:
        return jsonify({"success": False, "error": error})

    login_user(user)
    return jsonify({"success": True})

@app.route("/api/login", methods=["POST"])
def api_login():
    data       = request.get_json()
    identifier = data.get("identifier", "").strip()
    password   = data.get("password", "").strip()

    user, error = login_user_by_username(identifier, password)
    if error:
        return jsonify({"success": False, "error": error})

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
            "plan":      getattr(current_user, 'plan', 'free') or 'free'
        })
    return jsonify({"logged_in": False})

@app.route("/api/usage")
@login_required
def api_usage():
    summary = get_usage_summary(current_user.id, current_user.plan)
    return jsonify({
        "plan":    current_user.plan,
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
    plan    = getattr(current_user, 'plan', 'free') or 'free' \
              if current_user.is_authenticated else 'free'

    user_memories = get_user_memories(user_id) if user_id else None

    try:
        reply, switched, reset_ts, model_used = smart_chat(
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
def persona_chat():
    data           = request.get_json()
    user_message   = data.get("message", "")
    history        = data.get("history", [])
    persona_prompt = data.get("persona_prompt", "")

    user_id = current_user.id if current_user.is_authenticated else None
    plan    = getattr(current_user, 'plan', 'free') or 'free' \
              if current_user.is_authenticated else 'free'

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
        "plan":       u.plan,
        "created_at": u.created_at.isoformat()
    } for u in users])

@app.route("/api/admin/users/<int:user_id>/plan", methods=["POST"])
@login_required
@admin_required
def update_user_plan(user_id):
    data = request.get_json()
    plan = data.get("plan", "free")
    if plan not in PLAN_CONFIG:
        return jsonify({"success": False, "error": "پلان نامعتبر"})
    user = User.query.get_or_404(user_id)
    user.plan = plan
    db.session.commit()
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
@login_required
def tutor_chat_new():
    """Main tutor chat endpoint — saves full history per subject."""
    data       = request.get_json()
    subject    = data.get("subject")
    topic_key  = data.get("topic_key")
    level      = int(data.get("level", 1))
    message    = data.get("message", "")

    if subject not in CURRICULUM:
        return jsonify({"error": "مضمون پیدا نشد"}), 400

    # get topic info
    topic_info = None
    for t in CURRICULUM[subject]['levels'].get(level, {}).get('topics', []):
        if t['key'] == topic_key:
            topic_info = t
            break

    if not topic_info:
        return jsonify({"error": "موضوع پیدا نشد"}), 400

    progress = get_or_create_progress(current_user.id, subject)

    # load saved chat history
    chat_history = json.loads(progress.chat_history or '[]')

    # build system prompt
    system_prompt = build_tutor_system_prompt(
        subject, topic_info['title'],
        topic_info['desc'], len(chat_history)
    )

    plan = getattr(current_user, 'plan', 'free') or 'free'

    try:
        reply, switched, reset_ts, model_used = smart_chat(
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

    # save updated chat history
    chat_history.append({"role": "user", "content": message})
    chat_history.append({"role": "assistant", "content": reply})

    # keep last 60 messages to avoid DB bloat
    if len(chat_history) > 60:
        chat_history = chat_history[-60:]

    progress.chat_history    = json.dumps(chat_history, ensure_ascii=False)
    progress.last_activity   = datetime.utcnow()
    progress.last_topic_title = topic_info['title']
    db.session.commit()

    response = {"reply": reply}
    if switched:
        response["switch_notice"] = True
        if reset_ts:
            response["reset_ts"] = reset_ts

    return jsonify(response)

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

@app.route("/api/tutor/generate-quiz", methods=["POST"])
@login_required
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

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[{"role": "user", "content": quiz_prompt}],
            max_completion_tokens=2000,
            temperature=0.8
        )
        raw = response.choices[0].message.content.strip()
        # clean up any markdown fences
        raw = raw.replace('```json', '').replace('```', '').strip()
        quiz_data = json.loads(raw)
        return jsonify(quiz_data)
    except Exception as e:
        print(f"Quiz generation error: {e}")
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
@login_required
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

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=2000,
            temperature=0.7
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        return jsonify(json.loads(raw))
    except Exception as e:
        print(f"Placement test error: {e}")
        return jsonify({"error": "خطا در ساخت تست"}), 500

@app.route("/api/tutor/placement-result", methods=["POST"])
@login_required
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