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
import base64

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("ADMIN_PASSWORD", "fallback-secret")
database_url = os.getenv("DATABASE_URL", "sqlite:///khayyam.db")
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB max upload

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
groq_client   = Groq(api_key=os.getenv("GROQ_API_KEY"))

KNOWLEDGE_FILE = "knowledge.json"
EXAMPLES_FILE  = "examples.json"
TOKEN_LIMIT    = 5000   # tokens per window
RESET_HOURS    = 3

# token store: { identifier: {"tokens": N, "window_start": timestamp} }
token_store = {}

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

# ── TOKEN RATE LIMITING ──
def get_identifier():
    if current_user.is_authenticated:
        return f"user_{current_user.id}"
    return f"ip_{request.remote_addr}"

def check_token_limit(tokens_used):
    """
    Returns (is_limited, tokens_remaining, reset_timestamp_utc)
    is_limited = True means switch to Groq
    reset_timestamp_utc is a Unix timestamp the browser can convert to local time
    """
    key = get_identifier()
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    if key not in token_store:
        token_store[key] = {"tokens": 0, "window_start": now_ts}

    entry = token_store[key]
    hours_passed = (now_ts - entry["window_start"]) / 3600

    # reset window if expired
    if hours_passed >= RESET_HOURS:
        entry["tokens"]       = 0
        entry["window_start"] = now_ts

    reset_ts = entry["window_start"] + (RESET_HOURS * 3600)
    remaining = max(0, TOKEN_LIMIT - entry["tokens"])
    is_limited = entry["tokens"] >= TOKEN_LIMIT

    if not is_limited:
        entry["tokens"] += tokens_used

    return is_limited, remaining, reset_ts

def estimate_tokens(text):
    # rough estimate: 1 token ≈ 4 characters
    return len(text) // 4

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
قانون زبان
====================
زبان پیش‌فرض: دری افغانی

- اگر موضوع آموزشی زبان باشد، مثال‌ها را به همان زبان بنویس اما توضیحات را به دری بده
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
- جواب‌های طولانی را حتماً به بخش‌های جداگانه تقسیم کن

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
def call_openai(system_prompt, messages, temperature=0.7, image_b64=None, image_type=None):
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

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt}] + formatted,
        temperature=temperature,
        max_tokens=1000,
    )
    reply        = response.choices[0].message.content
    tokens_used  = response.usage.total_tokens
    return reply, tokens_used

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

def smart_chat(system_prompt, history, user_message, temperature=0.7,
               image_b64=None, image_type=None):
    """
    Returns (reply, used_groq, reset_timestamp, tokens_remaining)
    """
    messages = history + [{"role": "user", "content": user_message}]

    # images always need GPT-4o — estimate tokens first
    estimated = estimate_tokens(user_message) + 500  # +500 for system prompt estimate
    is_limited, remaining, reset_ts = check_token_limit(estimated)

    if is_limited:
        reply = call_groq(system_prompt, messages, temperature)
        return reply, True, reset_ts, 0

    try:
        reply, actual_tokens = call_openai(
            system_prompt, messages, temperature, image_b64, image_type
        )
        # update with actual token count
        key = get_identifier()
        if key in token_store:
            token_store[key]["tokens"] += (actual_tokens - estimated)
        remaining = max(0, TOKEN_LIMIT - token_store.get(key, {}).get("tokens", 0))
        return reply, False, reset_ts, remaining
    except Exception as e:
        # fallback to groq on any openai error
        reply = call_groq(system_prompt, messages, temperature)
        return reply, True, reset_ts, remaining

# ── DOCUMENT EXTRACTION ──
def extract_text_from_file(file_bytes, filename):
    ext = filename.lower().split('.')[-1]

    if ext == 'txt':
        return file_bytes.decode('utf-8', errors='ignore')

    if ext == 'pdf':
        try:
            import fitz  # PyMuPDF
            doc  = fitz.open(stream=file_bytes, filetype="pdf")
            text = ""
            for page in doc:
                text += page.get_text()
            return text[:8000]  # limit to 8000 chars
        except Exception as e:
            return f"خطا در خواندن PDF: {str(e)}"

    if ext in ['doc', 'docx']:
        try:
            import docx
            import io
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
    # handle both JSON and multipart form data
    if request.content_type and 'multipart/form-data' in request.content_type:
        user_message = request.form.get("message", "")
        history      = json.loads(request.form.get("history", "[]"))
        conv_id      = request.form.get("conversation_id")
        conv_id      = int(conv_id) if conv_id else None

        image_b64  = None
        image_type = None
        doc_text   = None

        if 'image' in request.files:
            img        = request.files['image']
            img_bytes  = img.read()
            image_b64  = base64.b64encode(img_bytes).decode('utf-8')
            image_type = img.content_type or 'image/jpeg'
            user_message = user_message or "این تصویر را به دری توضیح بده"

        if 'document' in request.files:
            doc       = request.files['document']
            doc_bytes = doc.read()
            doc_text  = extract_text_from_file(doc_bytes, doc.filename)
            user_message = (user_message or "این سند را خلاصه کن") + \
                           f"\n\n[محتوای فایل]:\n{doc_text}"
    else:
        data         = request.get_json()
        user_message = data.get("message", "")
        history      = data.get("history", [])
        conv_id      = data.get("conversation_id")
        image_b64    = None
        image_type   = None

    reply, used_groq, reset_ts, tokens_remaining = smart_chat(
        system_prompt=build_system_prompt(),
        history=history[-10:],
        user_message=user_message,
        temperature=0.7,
        image_b64=image_b64,
        image_type=image_type
    )

    response_data = {
        "reply":            reply,
        "tokens_remaining": tokens_remaining,
        "reset_timestamp":  reset_ts
    }

    if used_groq:
        response_data["limit_notice"] = True
        response_data["reset_timestamp"] = reset_ts

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

    reply, _, _, _ = smart_chat(
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

    reply, _, _, _ = smart_chat(
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