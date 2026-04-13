from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
from database import db, User, Conversation, Message
from auth import register_user, login_user_by_username
from datetime import datetime
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

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

KNOWLEDGE_FILE = "knowledge.json"
EXAMPLES_FILE  = "examples.json"

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

# ── DARI FILTER ──
def filter_non_dari(text):
    cleaned = re.sub(
        r'[^\u0600-\u06FF\u200c\u200d\s\d\.\،\؟\!\:\؛\-\(\)\[\]\"\'\/\\n\+\=\*\%]',
        '',
        text
    )
    cleaned = re.sub(r'  +', ' ', cleaned)
    cleaned = cleaned.strip()
    return cleaned if cleaned else text

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
  - مثال‌ها، جملات و تمرین‌ها را به همان زبان بنویس (مثلاً انگلیسی)
  - توضیحات را به دری بده
- اگر کاربر به زبان دیگری سوال کند، می‌توانی به همان زبان یا ترکیبی پاسخ بدهی
- در موضوعات علمی:
  - استفاده از نمادها و اصطلاحات بین‌المللی مجاز است (x, H2O, km, etc)

هدف:
→ کاربر باید بهتر یاد بگیرد، نه اینکه محدود شود

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
- در صورت نیاز از پاراگراف‌های کوتاه، لیست و تیتر استفاده کن

====================
حالت شعر و ادبیات
====================
اگر کاربر درباره شعر سوال کند:

1. اگر درخواست شعر از شاعر مشخص باشد:
   - اگر شعر را دقیق می‌دانی، آن را بنویس
   - اگر مطمئن نیستی، بگو: "مطمئن نیستم دقیق باشد، اما این یک نمونه است"
   - از ساختن شعر جعلی به نام شاعر واقعی خودداری کن

2. اگر کاربر بگوید "مثل فلان شاعر شعر بساز":
   - شعر جدید بساز، اما بگو "به سبک..."

3. اگر فقط بگوید "یک شعر بنویس":
   - شعر اصلی تولید کن

قوانین شعر:
- شعر باید روان، احساسی و ادبی باشد
- قالب دوبیتی یا آزاد مجاز است

⚠️ مهم:
- برای شاعران واقعی، ترجیح بده شعر واقعی بیاوری، نه ساختگی

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
{example_block}
"""

def build_tutor_prompt(subject, grade):
    knowledge = load_knowledge()
    dialect_rules = ""
    for item in knowledge.get("dari_dialect", []):
        dialect_rules += f'- بگو "{item["correct"]}" نه "{item["wrong"]}"\n'

    return f"""تو استاد خیام هستی — یک استاد افغانی مهربان که به دری افغانی درس می‌دهی.
فقط به دری افغانی جواب بده. هیچ زبان دیگری استفاده نکن.

مضمون: {subject}
سطح: {grade}

قوانین گویش:
{dialect_rules}

روش تدریس:
- مفاهیم را ساده و با مثال‌های افغانی توضیح بده
- بعد از هر توضیح یک سوال کوتاه برای امتحان فهم بپرس
- اگر جواب درست بود تشویق کن
- اگر جواب غلط بود با مهربانی تصحیح کن
- از کلمات افغانی مثل احسنت، آفرین، عالی استفاده کن"""

# ── GROQ ──
def openai_chat(system_prompt, history, user_message, temperature=0.6):
    messages = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=temperature,
        max_tokens=800,
    )

    return filter_non_dari(response.choices[0].message.content)
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

    reply = openai_chat(
        system_prompt=build_system_prompt(),
        history=history[-10:],
        user_message=user_message,
        temperature=0.6
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
        return jsonify({"reply": reply, "conversation_id": conv.id})

    return jsonify({"reply": reply})

# ── TUTOR API ──
@app.route("/api/tutor-chat", methods=["POST"])
def tutor_chat():
    data         = request.get_json()
    user_message = data.get("message", "")
    history      = data.get("history", [])
    subject      = data.get("subject", "عمومی")
    grade        = data.get("grade", "متوسط")

    reply = openai_chat(
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

    reply = openai_chat(
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