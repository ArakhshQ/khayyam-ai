from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
from database import db, User, Conversation, Message, Memory, UserTokenUsage
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

# ── PLAN CONFIGURATION ──
# Each plan defines a cascade of (model, limit, reset_period)
# reset_period: 'daily' or 'monthly'
# limit: token limit — None means unlimited

PLAN_CONFIG = {
    'free': [
        {'model': 'gpt-5.4-mini',           'tier': 1, 'limit': 3000,    'reset': 'daily'},
        {'model': 'gpt-5.4-nano',           'tier': 2, 'limit': 3000,    'reset': 'daily'},
        {'model': 'llama-3.3-70b-versatile','tier': 3, 'limit': None,    'reset': None},
    ],
    'basic': [
        {'model': 'gpt-5.4-mini',           'tier': 1, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',           'tier': 2, 'limit': 200000,  'reset': 'monthly'},
        {'model': 'llama-3.3-70b-versatile','tier': 3, 'limit': None,    'reset': None},
    ],
    'pro': [
        {'model': 'gpt-5.4-mini',           'tier': 1, 'limit': 500000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',           'tier': 2, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'llama-3.3-70b-versatile','tier': 3, 'limit': None,    'reset': None},
    ],
    'premium': [
        {'model': 'gpt-5.4',                'tier': 1, 'limit': 2000000, 'reset': 'monthly'},
        {'model': 'gpt-5.4-mini',           'tier': 2, 'limit': 500000,  'reset': 'monthly'},
        {'model': 'gpt-5.4-nano',           'tier': 3, 'limit': 300000,  'reset': 'monthly'},
        {'model': 'llama-3.3-70b-versatile','tier': 4, 'limit': None,    'reset': None},
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

# ── TOKEN USAGE (DATABASE BACKED) ──
def get_or_create_usage(user_id):
    usage = UserTokenUsage.query.filter_by(user_id=user_id).first()
    if not usage:
        usage = UserTokenUsage(user_id=user_id)
        db.session.add(usage)
        db.session.commit()
    return usage

def should_reset(reset_at, period):
    if period is None:
        return False
    now = datetime.utcnow()
    if period == 'daily':
        return (now - reset_at).total_seconds() >= 86400
    if period == 'monthly':
        return (now - reset_at).total_seconds() >= 2592000  # 30 days
    return False

def get_reset_timestamp(reset_at, period):
    if period == 'daily':
        reset_time = reset_at + timedelta(days=1)
    elif period == 'monthly':
        reset_time = reset_at + timedelta(days=30)
    else:
        return None
    return reset_time.replace(tzinfo=timezone.utc).timestamp()

def pick_model_and_update(user_id, plan, tokens_to_use):
    """
    Picks the right model based on plan and current usage.
    Updates token counts in DB.
    Returns (model_string, tier_index, reset_timestamp, switched)
    switched=True means we moved to a fallback tier
    """
    cascade  = PLAN_CONFIG.get(plan, PLAN_CONFIG['free'])
    usage    = get_or_create_usage(user_id)
    switched = False

    for i, tier in enumerate(cascade):
        model  = tier['model']
        limit  = tier['limit']
        period = tier['reset']
        t      = tier['tier']

        # unlimited tier — always use this
        if limit is None:
            if i > 0:
                switched = True
            return model, t, None, switched

        # get current usage for this tier
        tokens_used = getattr(usage, f'tier{t}_tokens', 0)
        reset_at    = getattr(usage, f'tier{t}_reset', datetime.utcnow())

        # check if reset needed
        if should_reset(reset_at, period):
            setattr(usage, f'tier{t}_tokens', 0)
            setattr(usage, f'tier{t}_reset', datetime.utcnow())
            tokens_used = 0

        # check if within limit
        if tokens_used + tokens_to_use <= limit:
            # use this tier
            setattr(usage, f'tier{t}_tokens', tokens_used + tokens_to_use)
            usage.updated_at = datetime.utcnow()
            db.session.commit()
            reset_ts = get_reset_timestamp(reset_at, period)
            if i > 0:
                switched = True
            return model, t, reset_ts, switched

        # limit exceeded — try next tier
        if i > 0:
            switched = True

    # all tiers exhausted — use last tier (llama)
    last = cascade[-1]
    return last['model'], last['tier'], None, True

def get_usage_summary(user_id, plan):
    cascade = PLAN_CONFIG.get(plan, PLAN_CONFIG['free'])
    usage   = get_or_create_usage(user_id)
    summary = []

    for tier in cascade:
        t      = tier['tier']
        limit  = tier['limit']
        period = tier['reset']

        if limit is None:
            continue

        tokens_used = getattr(usage, f'tier{t}_tokens', 0)
        reset_at    = getattr(usage, f'tier{t}_reset', datetime.utcnow())

        if should_reset(reset_at, period):
            tokens_used = 0

        reset_ts = get_reset_timestamp(reset_at, period)
        summary.append({
            'model':      tier['model'],
            'used':       tokens_used,
            'limit':      limit,
            'reset_ts':   reset_ts,
            'period':     period,
            'remaining':  max(0, limit - tokens_used)
        })

    return summary

# ── KNOWLEDGE ──
def load_knowledge():
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"dari_dialect": [], "cultural_customs": []}

def load_examples():
    try:
        with open(EXAMPLES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"conversation_examples": []}

def save_knowledge(data):
    with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_examples(data):
    with open(EXAMPLES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── MEMORY ──
def get_user_memories(user_id):
    memories = Memory.query.filter_by(user_id=user_id).order_by(Memory.created_at.desc()).all()
    return [m.content for m in memories]

def extract_and_save_memory(user_id, user_message):
    trigger_words = [
        'یادت باشد', 'ذخیره کن', 'به یاد داشته باش', 'فراموش نکن',
        'remember', 'save this', 'note that', 'keep in mind',
        'بدان که', 'حفظ کن'
    ]
    if not any(word in user_message for word in trigger_words):
        return

    try:
        extraction_prompt = f"""کاربر این پیام را فرستاده:
"{user_message}"

اگر کاربر خواسته چیزی به یاد سپرده شود، آن را یک جمله کوتاه بنویس.
اگر نه، فقط بنویس: NONE"""

        response = openai_client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=[{"role": "user", "content": extraction_prompt}],
            max_tokens=80,
            temperature=0.1
        )
        memory_text = response.choices[0].message.content.strip()

        if memory_text and memory_text != "NONE" and len(memory_text) > 3:
            existing = Memory.query.filter_by(user_id=user_id, content=memory_text).first()
            if not existing:
                db.session.add(Memory(user_id=user_id, content=memory_text))
                db.session.commit()
    except Exception as e:
        print(f"Memory extraction error: {e}")

# ── DARI FILTER ──
def filter_non_dari(text):
    cleaned = re.sub(
        r'[^\u0600-\u06FF\u200c\u200d\s\d\.\،\؟\!\:\؛\-\(\)\[\]\"\'\/\n\+\=\*\%\#]',
        '',
        text
    )
    cleaned = re.sub(r'  +', ' ', cleaned)
    cleaned = cleaned.strip()
    return cleaned if cleaned else text

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
        memory_block = "====================\nحافظه کاربر\n====================\n"
        for mem in user_memories:
            memory_block += f"- {mem}\n"
        memory_block += "\n"

    return f"""تو یک دستیار هوشمند به نام خیام هستی که به زبان دری افغانی صحبت می‌کنی.

{memory_block}
====================
قانون زبان
====================
زبان پیش‌فرض: دری افغانی
- اگر موضوع آموزش زبان باشد، مثال‌ها را به همان زبان بنویس اما توضیحات را به دری بده
- در موضوعات علمی استفاده از نمادها مجاز است (x, H2O, km)
- اگر کاربر لینک فرستاد بگو نمی‌توانی باز کنی
- اگر کاربر تصویر یا فایل فرستاد، آن را به دری توضیح بده

====================
هویت و شخصیت
====================
- نام: خیام
- لحن: گرم، مهربان و صمیمی
- از کلمات مانند: برادر، خواهر، تشکر استفاده کن
- هرگز خود را ChatGPT معرفی نکن

====================
فرمت‌بندی (بسیار مهم)
====================
همیشه از Markdown استفاده کن:
- **متن مهم** را بولد کن
- برای لیست از - استفاده کن
- برای تیتر از ## استفاده کن
- پاراگراف‌ها را با خط خالی جدا کن
- جواب‌های طولانی را به بخش‌های جداگانه تقسیم کن

====================
حالت ویژه: شعر
====================
وقتی شعر می‌نویسی:
- هر مصرع روی یک خط جداگانه
- بین هر دو بیت یک خط خالی
- قافیه را در تمام شعر حفظ کن
- اگر از شاعر واقعی و مطمئن نیستی بگو "به سبک..."

فرمت اجباری:
مصرع اول
مصرع دوم

مصرع سوم
مصرع چهارم

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
    knowledge = load_knowledge()
    dialect_rules = ""
    for item in knowledge.get("dari_dialect", []):
        dialect_rules += f'- بگو "{item["correct"]}" نه "{item["wrong"]}"\n'

    return f"""تو استاد خیام هستی — یک استاد افغانی مهربان که به دری افغانی درس می‌دهی.
فقط به دری افغانی جواب بده.

مضمون: {subject}
سطح: {grade}

قوانین گویش:
{dialect_rules}

فرمت: از Markdown استفاده کن. مفاهیم را با **بولد** و لیست واضح کن.

روش تدریس:
- مفاهیم را ساده و با مثال‌های افغانی توضیح بده
- بعد از هر توضیح یک سوال کوتاه بپرس
- اگر جواب درست بود تشویق کن
- اگر جواب غلط بود با مهربانی تصحیح کن
- از کلمات مثل احسنت، آفرین، عالی استفاده کن"""

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

    # gpt-5.4 family uses max_completion_tokens
    # older models use max_tokens
    is_new_model = any(x in model for x in ['gpt-5', 'o1', 'o3', 'o4'])
    token_param  = 'max_completion_tokens' if is_new_model else 'max_tokens'

    kwargs = {
        'model':       model,
        'messages':    [{"role": "system", "content": system_prompt}] + formatted,
        token_param:   1000,
    }

    # new models dont support temperature with reasoning effort none
    if not is_new_model:
        kwargs['temperature'] = temperature

    response = openai_client.chat.completions.create(**kwargs)
    reply       = response.choices[0].message.content
    tokens_used = response.usage.total_tokens
    return reply, tokens_used

def call_groq_model(system_prompt, messages, temperature=0.7):
    msgs = [{"role": "system", "content": system_prompt}] + messages
    for model in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]:
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
            if "rate_limit" in str(e).lower() or "429" in str(e):
                continue
            raise e
    return "متأسفم، سرور مصروف است. لطفاً دوباره امتحان کنید.", 0

def smart_chat(system_prompt, history, user_message, user_id=None, plan='free',
               temperature=0.7, image_b64=None, image_type=None):
    """
    Returns (reply, switched, reset_timestamp, model_used)
    switched=True means fell back to a lower tier
    """
    messages       = history + [{"role": "user", "content": user_message}]
    estimated_tokens = len(user_message) // 4 + 300

    if user_id is None:
        # guest — use nano directly, no tracking
        try:
            reply, _ = call_openai_model(
                'gpt-5.4-nano', system_prompt, messages,
                temperature, image_b64, image_type
            )
            return reply, False, None, 'gpt-5.4-nano'
        except:
            reply, _ = call_groq_model(system_prompt, messages, temperature)
            return reply, True, None, 'llama'

    model, tier, reset_ts, switched = pick_model_and_update(
        user_id, plan, estimated_tokens
    )

    # groq model — free tier
    if 'llama' in model:
        reply, _ = call_groq_model(system_prompt, messages, temperature)
        return reply, switched, reset_ts, model

    # openai model
    try:
        reply, actual_tokens = call_openai_model(
            model, system_prompt, messages,
            temperature, image_b64, image_type
        )
        # adjust token count with actual usage
        if actual_tokens > estimated_tokens:
            diff = actual_tokens - estimated_tokens
            usage = get_or_create_usage(user_id)
            current = getattr(usage, f'tier{tier}_tokens', 0)
            setattr(usage, f'tier{tier}_tokens', current + diff)
            db.session.commit()
        return reply, switched, reset_ts, model
    except Exception as e:
        print(f"OpenAI error: {e}")
        # fallback to groq
        reply, _ = call_groq_model(system_prompt, messages, temperature)
        return reply, True, reset_ts, 'llama'

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
            img        = request.files['image']
            img_bytes  = img.read()
            image_b64  = base64.b64encode(img_bytes).decode('utf-8')
            image_type = img.content_type or 'image/jpeg'
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
    plan = getattr(current_user, 'plan', 'free') or 'free' if current_user.is_authenticated else 'free'

    user_memories = get_user_memories(user_id) if user_id else None

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

    if user_id:
        extract_and_save_memory(user_id, user_message)

    response_data = {"reply": reply}

    if switched and reset_ts:
        response_data["switch_notice"] = True
        response_data["reset_ts"]      = reset_ts

    if current_user.is_authenticated:
        if conv_id:
            conv = Conversation.query.filter_by(
                id=conv_id, user_id=current_user.id
            ).first()
        else:
            conv = None

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

# ── TUTOR API ──
@app.route("/api/tutor-chat", methods=["POST"])
def tutor_chat():
    data         = request.get_json()
    user_message = data.get("message", "")
    history      = data.get("history", [])
    subject      = data.get("subject", "عمومی")
    grade        = data.get("grade", "متوسط")

    user_id = current_user.id if current_user.is_authenticated else None
    plan = getattr(current_user, 'plan', 'free') or 'free' if current_user.is_authenticated else 'free'

    reply, _, _, _ = smart_chat(
        system_prompt=build_tutor_prompt(subject, grade),
        history=history[-12:],
        user_message=user_message,
        user_id=user_id,
        plan=plan,
        temperature=0.6
    )
    return jsonify({"reply": reply})

# ── PERSONA API ──
@app.route("/api/persona-chat", methods=["POST"])
def persona_chat():
    data           = request.get_json()
    user_message   = data.get("message", "")
    history        = data.get("history", [])
    persona_prompt = data.get("persona_prompt", "")

    user_id = current_user.id if current_user.is_authenticated else None
    plan = getattr(current_user, 'plan', 'free') or 'free' if current_user.is_authenticated else 'free'

    reply, _, _, _ = smart_chat(
        system_prompt=persona_prompt,
        history=history[-10:],
        user_message=user_message,
        user_id=user_id,
        plan=plan,
        temperature=0.9
    )
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

if __name__ == "__main__":
    app.run(debug=True)