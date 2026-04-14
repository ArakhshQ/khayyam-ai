from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
from database import db, User, Conversation, Message
from auth import register_user, login_user_by_username
from datetime import datetime, timezone
import os
import json
import re

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("ADMIN_PASSWORD", "fallback-secret")
database_url = os.getenv("DATABASE_URL", "sqlite:///khayyam.db")
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
groq_client   = Groq(api_key=os.getenv("GROQ_API_KEY"))

KNOWLEDGE_FILE   = "knowledge.json"
EXAMPLES_FILE    = "examples.json"
DAILY_LIMIT      = 10
RESET_HOURS      = 3

# in-memory rate limit store: { identifier: {"count": N, "window_start": datetime} }
rate_store = {}

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

# ── RATE LIMITING ──
def get_identifier():
    if current_user.is_authenticated:
        return f"user_{current_user.id}"
    return f"ip_{request.remote_addr}"

def check_and_increment_limit():
    """
    Returns (is_limited, used, reset_time_str)
    is_limited = True means GPT-4o limit hit, switch to Groq
    """
    key = get_identifier()
    now = datetime.now(timezone.utc)

    if key not in rate_store:
        rate_store[key] = {"count": 0, "window_start": now}

    entry = rate_store[key]
    window_start = entry["window_start"]

    # check if window has expired
    hours_passed = (now - window_start).total_seconds() / 3600
    if hours_passed >= RESET_HOURS:
        entry["count"]        = 0
        entry["window_start"] = now

    # calculate reset time
    reset_at     = window_start.replace(tzinfo=timezone.utc) if window_start.tzinfo is None else window_start
    reset_at     = reset_at.timestamp() + (RESET_HOURS * 3600)
    reset_dt     = datetime.fromtimestamp(reset_at, tz=timezone.utc)
    reset_str    = reset_dt.strftime("%H:%M") + " UTC"

    is_limited = entry["count"] >= DAILY_LIMIT

    if not is_limited:
        entry["count"] += 1

    return is_limited, entry["count"], reset_str

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

# ── PROMPTS ──
def build_system_prompt():
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

    return f"""تو یک دستیار هوشمند به نام خیام هستی که به زبان دری افغانی صحبت می‌کنی.

====================
قانون زبان (بسیار مهم)
====================
زبان پیش‌فرض: دری افغانی

اما:
- اگر موضوع آموزشی زبان (مثل انگلیسی) باشد:
  - مثال‌ها، جملات و تمرین‌ها را به همان زبان بنویس
  - توضیحات را به دری بده
- در موضوعات علمی استفاده از نمادها و اصطلاحات بین‌المللی مجاز است (x, H2O, km, etc)
- اگر کاربر لینکی فرستاد، بگو که نمی‌توانی لینک‌ها را باز کنی

====================
هویت و شخصیت
====================
- نام: خیام
- لحن: گرم، مهربان و صمیمی
- استفاده از کلمات مانند: برادر، خواهر، تشکر
- پاسخ‌ها طبیعی و انسانی باشند
- هرگز خود را ChatGPT معرفی نکن

====================
رفتار عمومی
====================
- به هر نوع سوال جواب بده
- پاسخ‌ها واضح، دقیق و قابل فهم باشند

====================
فرمت‌بندی (بسیار مهم)
====================
همیشه از Markdown استفاده کن:
- **متن مهم** را بولد کن
- برای لیست از - استفاده کن
- برای تیتر از ## استفاده کن
- پاراگراف‌ها را با یک خط خالی جدا کن
- جواب‌های طولانی را حتماً به بخش‌های جداگانه تقسیم کن

====================
حالت ویژه: شعر
====================
وقتی شعر می‌نویسی یا می‌آوری:
- هر مصرع روی یک خط جداگانه
- بین هر دو بیت یک خط خالی
- قافیه را در تمام شعر حفظ کن
- اگر شعر از شاعر واقعی است و مطمئن نیستی، بگو "نمونه‌ای به سبک..."
- اگر کاربر فقط گفت شعر بنویس — شعر اصیل تولید کن

فرمت اجباری شعر:
مصرع اول بیت اول
مصرع دوم بیت اول

مصرع اول بیت دوم
مصرع دوم بیت دوم

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

فرمت‌بندی:
- از Markdown استفاده کن
- مفاهیم را با **بولد** و لیست واضح کن

روش تدریس:
- مفاهیم را ساده و با مثال‌های افغانی توضیح بده
- بعد از هر توضیح یک سوال کوتاه برای امتحان فهم بپرس
- اگر جواب درست بود تشویق کن
- اگر جواب غلط بود با مهربانی تصحیح کن
- از کلمات افغانی مثل احسنت، آفرین، عالی استفاده کن"""

# ── MODEL ROUTING ──
def choose_model(message):
    poetry_words  = ['شعر', 'رباعی', 'غزل', 'مثنوی', 'دوبیتی', 'قصیده']
    complex_words = ['توضیح', 'تحلیل', 'مقایسه', 'تاریخ', 'فلسفه', 'علمی',
                     'ریاضی', 'فزیک', 'کیمیا', 'چرا', 'چطور', 'بنویس', 'بساز']

    if any(w in message for w in poetry_words):
        return "gpt-4o"
    if any(w in message for w in complex_words):
        return "gpt-4o"
    if len(message) > 80:
        return "gpt-4o"
    return "gpt-3.5-turbo"

# ── CHAT FUNCTIONS ──
def call_openai(system_prompt, messages, temperature=0.7):
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt}] + messages,
        temperature=temperature,
        max_tokens=1000,
    )
    return response.choices[0].message.content

def call_groq(system_prompt, messages, temperature=0.7):
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
            return response.choices[0].message.content
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                continue
            raise e
    return "متأسفم، سرور مصروف است. لطفاً دوباره امتحان کنید."

def smart_chat(system_prompt, history, user_message, temperature=0.7, force_groq=False):
    """
    Returns (reply, used_groq, reset_time)
    used_groq=True means we switched to fallback
    """
    messages = history + [{"role": "user", "content": user_message}]

    if force_groq:
        return call_groq(system_prompt, messages, temperature), True, None

    is_limited, count, reset_str = check_and_increment_limit()

    if is_limited:
        reply = call_groq(system_prompt, messages, temperature)
        return reply, True, reset_str

    reply = call_openai(system_prompt, messages, temperature)
    return reply, False, None

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
            "is_admin":  current_user.is_admin
        })
    return jsonify({"logged_in": False})

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
    data         = request.get_json()
    user_message = data.get("message", "")
    history      = data.get("history", [])
    conv_id      = data.get("conversation_id")

    reply, used_groq, reset_time = smart_chat(
        system_prompt=build_system_prompt(),
        history=history[-10:],
        user_message=user_message,
        temperature=0.7
    )

    # build response
    response_data = {"reply": reply}

    if used_groq and reset_time:
        response_data["limit_notice"] = (
            f"⚡ سقف ۱۰ پیام خیام اصلی تمام شد. "
            f"اکنون با نسخه پایه‌تر صحبت می‌کنید. "
            f"محدودیت در ساعت {reset_time} بازنشینی می‌شود."
        )

    if current_user.is_authenticated:
        if conv_id:
            conv = Conversation.query.filter_by(
                id=conv_id,
                user_id=current_user.id
            ).first()
        else:
            conv = None

        if not conv:
            title = user_message[:60] if len(user_message) > 0 else "گفتگوی جدید"
            conv  = Conversation(user_id=current_user.id, title=title)
            db.session.add(conv)
            db.session.flush()

        db.session.add(Message(
            conversation_id=conv.id,
            role='user',
            content=user_message
        ))
        db.session.add(Message(
            conversation_id=conv.id,
            role='assistant',
            content=reply
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

    reply, used_groq, reset_time = smart_chat(
        system_prompt=build_tutor_prompt(subject, grade),
        history=history[-12:],
        user_message=user_message,
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

    reply, _, _ = smart_chat(
        system_prompt=persona_prompt,
        history=history[-10:],
        user_message=user_message,
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
        id=conv_id,
        user_id=current_user.id
    ).first_or_404()
    return jsonify(conv.to_dict())

@app.route("/api/conversations/<int:conv_id>", methods=["DELETE"])
@login_required
def delete_conversation(conv_id):
    conv = Conversation.query.filter_by(
        id=conv_id,
        user_id=current_user.id
    ).first_or_404()
    db.session.delete(conv)
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
        "created_at": u.created_at.isoformat()
    } for u in users])

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