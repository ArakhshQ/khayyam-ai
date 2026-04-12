from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from groq import Groq
from dotenv import load_dotenv
from database import db, User, Conversation, Message
from auth import register_user, login_user_by_username, hash_password
from datetime import datetime
import os
import json

load_dotenv()

app = Flask(__name__)
app.secret_key        = os.getenv("ADMIN_PASSWORD", "fallback-secret")
database_url = os.getenv("DATABASE_URL", "sqlite:///khayyam.db")
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

ADMIN_PASSWORD   = os.getenv("ADMIN_PASSWORD")
ADMIN_SECRET_URL = os.getenv("ADMIN_SECRET_URL", "admin-secret")
KNOWLEDGE_FILE   = "knowledge.json"
EXAMPLES_FILE    = "examples.json"

# ── CREATE TABLES ──
with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

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

    return freturn f"""تو یک دستیار هوشمند افغانی هستی به نام خیام.

!!!! قانون مطلق — هرگز نقض نشود !!!!
تمام جواب را فقط و فقط به زبان دری افغانی بنویس.
حتی یک کلمه از زبان دیگر استفاده نکن.
نه انگلیسی، نه آلمانی، نه ترکی، نه چینی، نه عربی، نه فارسی ایرانی.
فقط دری افغانی. اگر کلمه‌ای را به دری نمی‌دانی، معادل دری آن را بنویس.
این قانون را در هر جمله، هر کلمه، هر حرف رعایت کن.

قوانین زبانی:
- فقط به زبان دری افغانی صحبت کن
- حتی اگر کسی به انگلیسی یا پشتو بنویسد، به دری افغانی جواب بده
- از این کلمات دری افغانی استفاده کن:
{dialect_rules}

قوانین فرمت‌بندی جواب:
- جواب‌های طولانی را به پاراگراف‌های کوتاه تقسیم کن
- وقتی چند مورد داری از لیست استفاده کن
- وقتی موضوعات مختلف داری از تیتر کوچک استفاده کن
- هرگز همه چیز را در یک پاراگراف ندهی
- جواب را ساده، خوانا و منظم بنویس

دانش فرهنگی افغانستان:
{cultural_knowledge}

شخصیت تو:
- گرم، مهربان و صمیمی مثل یک افغان واقعی
- از کلمات "برادر"، "خواهر"، "احسنت"، "تشکر" استفاده کن
- افغانستان، فرهنگ، تاریخ و مردمش را دوست داری
- هرگز خود را ChatGPT یا هوش مصنوعی ایرانی معرفی نکن
- اسم تو خیام است
- وقتی کسی پرسید کی هستی بگو: من خیام هستم، دستیار هوشمند افغانی

نمونه‌های گفتگو — دقیقاً به همین سبک صحبت کن:
{example_block}"""

def build_tutor_prompt(subject, grade):
    knowledge = load_knowledge()
    dialect_rules = ""
    for item in knowledge.get("dari_dialect", []):
        dialect_rules += f'- بگو "{item["correct"]}" نه "{item["wrong"]}"\n'

    return f"""تو استاد خیام هستی — یک استاد افغانی مهربان که به دری افغانی درس می‌دهی.
مضمون: {subject}
سطح: {grade}
قوانین زبانی:
- فقط دری افغانی، هرگز پشتو یا فارسی ایرانی
{dialect_rules}
روش تدریس:
- مفاهیم را ساده و با مثال‌های افغانی توضیح بده
- بعد از هر توضیح یک سوال کوتاه برای امتحان فهم بپرس
- اگر جواب درست بود تشویق کن
- اگر جواب غلط بود با مهربانی تصحیح کن
- از کلمات افغانی مثل آفرین، عالی استفاده کن"""

# ── GROQ ──
def groq_chat(system_prompt, history, user_message, temperature=0.8):
    messages = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": user_message})
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=1000,
        temperature=temperature
    )
    return response.choices[0].message.content

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
            "phone":     current_user.phone
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

@app.route("/persona")
def persona_page():
    return render_template("persona_chat.html")

@app.route("/tutor")
def tutor_page():
    return render_template("tutor.html")

# ── CHAT API ──
@app.route("/api/chat", methods=["POST"])
def chat():
    data         = request.get_json()
    user_message = data.get("message", "")
    history      = data.get("history", [])
    conv_id      = data.get("conversation_id")

    reply = groq_chat(
        system_prompt=build_system_prompt(),
        history=history[-10:],
        user_message=user_message,
        temperature=0.8
    )

    # save to database if user is logged in
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

    reply = groq_chat(
        system_prompt=build_tutor_prompt(subject, grade),
        history=history[-12:],
        user_message=user_message,
        temperature=0.7
    )
    return jsonify({"reply": reply})

# ── PERSONA API ──
@app.route("/api/persona-chat", methods=["POST"])
def persona_chat():
    data           = request.get_json()
    user_message   = data.get("message", "")
    history        = data.get("history", [])
    persona_prompt = data.get("persona_prompt", "")

    reply = groq_chat(
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

# ── ADMIN ROUTES ──
@app.route(f"/{ADMIN_SECRET_URL}")
def admin_login_page():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_panel"))
    return render_template("admin_login.html")

@app.route(f"/{ADMIN_SECRET_URL}/login", methods=["POST"])
def admin_login():
    data     = request.get_json()
    password = data.get("password", "")
    if password == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "رمز عبور اشتباه است"})

@app.route(f"/{ADMIN_SECRET_URL}/panel")
def admin_panel():
    if not session.get("admin_logged_in"):
        return redirect(f"/{ADMIN_SECRET_URL}")
    return render_template("admin_panel.html")

@app.route(f"/{ADMIN_SECRET_URL}/logout")
def admin_logout():
    session.clear()
    return redirect(f"/{ADMIN_SECRET_URL}")

# ── ADMIN APIs ──
@app.route("/api/admin/knowledge", methods=["GET"])
def get_knowledge():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(load_knowledge())

@app.route("/api/admin/examples", methods=["GET"])
def get_examples():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(load_examples())

@app.route("/api/admin/dialect", methods=["POST"])
def add_dialect():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
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
def delete_dialect(index):
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    knowledge = load_knowledge()
    if 0 <= index < len(knowledge["dari_dialect"]):
        knowledge["dari_dialect"].pop(index)
        save_knowledge(knowledge)
    return jsonify({"success": True})

@app.route("/api/admin/culture", methods=["POST"])
def add_culture():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    data      = request.get_json()
    knowledge = load_knowledge()
    knowledge["cultural_customs"].append({
        "topic":   data["topic"],
        "content": data["content"]
    })
    save_knowledge(knowledge)
    return jsonify({"success": True})

@app.route("/api/admin/culture/<int:index>", methods=["DELETE"])
def delete_culture(index):
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    knowledge = load_knowledge()
    if 0 <= index < len(knowledge["cultural_customs"]):
        knowledge["cultural_customs"].pop(index)
        save_knowledge(knowledge)
    return jsonify({"success": True})

@app.route("/api/admin/example", methods=["POST"])
def add_example():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    data     = request.get_json()
    examples = load_examples()
    examples["conversation_examples"].append({
        "user":      data["user"],
        "assistant": data["assistant"]
    })
    save_examples(examples)
    return jsonify({"success": True})

@app.route("/api/admin/example/<int:index>", methods=["DELETE"])
def delete_example(index):
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    examples = load_examples()
    if 0 <= index < len(examples["conversation_examples"]):
        examples["conversation_examples"].pop(index)
        save_examples(examples)
    return jsonify({"success": True})

if __name__ == "__main__":
    app.run(debug=True)